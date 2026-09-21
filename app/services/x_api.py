from typing import Protocol

import httpx

from app.config import settings
from app.services.x_oauth import build_authorization_header


class XApiError(Exception):
    """X API 调用失败（网络错误或非 2xx 响应）。"""


class XApiConfigError(RuntimeError):
    """缺少调用所需的凭据。

    刻意**不继承** XApiError：XApiError 会被 HTTP 层映射成 502（上游故障），
    而这是本服务自己的配置问题，应该 500。它在任何 HTTP 请求发出之前抛出，
    因此可以确定"什么都没发出去"——发布路径据此判定可以重试。
    """


class XApiResponseError(XApiError):
    """X 返回了响应，但状态码 >= 400（或 2xx 但内容无法解释）。

    带上 status_code 是发布路径的硬需求：4xx 表示 X 明确拒绝、没有创建内容
    （可以判"没发出去"），5xx 表示 X 可能已经创建再报错（只能判"结果未知"）。
    没有这个字段就只能从错误字符串里正则抠数字，那是不可维护的做法。
    """

    def __init__(self, status_code: int, message: str):
        super().__init__(message)
        self.status_code = status_code


class XApiTransportError(XApiError):
    """没有得到任何响应（网络层失败）。

    `may_have_reached_x` 是发布路径唯一关心的信息：
    - False（连接就没建立起来，ConnectError / ConnectTimeout）→ 确定对方不会
      创建内容，可以判"没发出去"
    - True（读超时 / 写中断等）→ 请求可能已经送达并被处理，只能保持"结果未知"

    默认取 True：不确定时一律按"可能发出去了"处理，这是唯一安全的方向。
    """

    def __init__(self, message: str, *, may_have_reached_x: bool = True):
        super().__init__(message)
        self.may_have_reached_x = may_have_reached_x


class XApiClient(Protocol):
    async def search_recent_tweets(self, query: str, max_results: int = 10) -> dict:
        """返回 /2/tweets/search/recent 的原始 JSON dict（data/includes/meta）。"""

    async def create_post(self, text: str) -> str:
        """发布一条推文，返回 X 侧的 post id。"""


class RealXApiClient:
    """基于官方 X API v2 的客户端。

    读接口（search）用 app-only Bearer token；写接口（create_post）必须用
    OAuth 1.0a user context——Bearer token 只能读，发帖会被 X 拒绝。

    字段映射依据官方 v2 Tweet/User 对象：id / text / author_id / created_at /
    public_metrics{retweet_count, reply_count, like_count, quote_count}。
    """

    def __init__(
        self,
        bearer_token: str | None = None,
        base_url: str | None = None,
        timeout: float = 30.0,
        http_client: httpx.AsyncClient | None = None,
        consumer_key: str | None = None,
        consumer_secret: str | None = None,
        access_token: str | None = None,
        access_token_secret: str | None = None,
    ):
        self._bearer_token = bearer_token or settings.x_api_bearer_token
        self._base_url = (base_url or settings.x_api_base_url).rstrip("/")
        self._timeout = timeout
        self._http_client = http_client
        self._consumer_key = consumer_key or settings.x_api_key
        self._consumer_secret = consumer_secret or settings.x_api_secret
        self._access_token = access_token or settings.x_access_token
        self._access_token_secret = access_token_secret or settings.x_access_token_secret

    async def _request(self, method: str, url: str, **kwargs) -> httpx.Response:
        """统一的 HTTP 收口：把 httpx 的异常翻译成本模块的错误类型。

        翻译规则只围绕一个问题——"请求有没有可能已经送达 X"。
        """
        client = self._http_client
        should_close = client is None
        if client is None:
            client = httpx.AsyncClient(timeout=self._timeout)
        try:
            try:
                return await client.request(method, url, **kwargs)
            except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
                # 连接都没建立起来 → 请求不可能送达
                raise XApiTransportError(
                    f"X API request could not be sent: {exc}", may_have_reached_x=False
                ) from exc
            except httpx.HTTPError as exc:
                # 读超时 / 写中断等：请求可能已经送达并被处理
                raise XApiTransportError(
                    f"X API request failed: {exc}", may_have_reached_x=True
                ) from exc
        finally:
            if should_close:
                await client.aclose()

    async def search_recent_tweets(self, query: str, max_results: int = 10) -> dict:
        url = f"{self._base_url}/2/tweets/search/recent"
        params = {
            "query": query,
            "max_results": max_results,
            "tweet.fields": "author_id,created_at,public_metrics",
            "expansions": "author_id",
            "user.fields": "username,name",
        }
        headers = {"Authorization": f"Bearer {self._bearer_token}"}

        resp = await self._request("GET", url, params=params, headers=headers)
        if resp.status_code >= 400:
            raise XApiResponseError(
                resp.status_code, f"X API returned {resp.status_code}: {resp.text[:200]}"
            )
        return self._parse_json(resp)

    async def create_post(self, text: str) -> str:
        """POST /2/tweets（OAuth 1.0a user context）。

        正文以 JSON 发送，**不参与签名**——OAuth 1.0a 只把 form-encoded 正文
        计入签名参数集，把 JSON body 塞进签名会导致 X 端 401。
        """
        self._require_oauth_credentials()
        url = f"{self._base_url}/2/tweets"
        headers = {
            "Authorization": build_authorization_header(
                "POST",
                url,
                consumer_key=self._consumer_key,
                consumer_secret=self._consumer_secret,
                token=self._access_token,
                token_secret=self._access_token_secret,
            ),
            "Content-Type": "application/json",
        }

        resp = await self._request("POST", url, json={"text": text}, headers=headers)
        if resp.status_code >= 400:
            raise XApiResponseError(
                resp.status_code, f"X API returned {resp.status_code}: {resp.text[:200]}"
            )
        data = self._parse_json(resp)
        post_id = (data.get("data") or {}).get("id")
        if not post_id:
            # 2xx 但拿不到 id：请求确实处理过了，结果却无法判定 →
            # 用带 2xx 状态码的 XApiResponseError 表达，调用方会归入"结果未知"
            raise XApiResponseError(
                resp.status_code, f"X API response has no data.id: {resp.text[:200]}"
            )
        return str(post_id)

    def _require_oauth_credentials(self) -> None:
        missing = [
            name
            for name, value in (
                ("X_API_KEY", self._consumer_key),
                ("X_API_SECRET", self._consumer_secret),
                ("X_ACCESS_TOKEN", self._access_token),
                ("X_ACCESS_TOKEN_SECRET", self._access_token_secret),
            )
            if not value
        ]
        if missing:
            raise XApiConfigError(
                "missing OAuth 1.0a credentials for publishing: " + ", ".join(missing)
            )

    @staticmethod
    def _parse_json(resp: httpx.Response) -> dict:
        try:
            return resp.json()
        except ValueError as exc:
            # 非 JSON 响应体（HTML / 空体）。2xx 走到这里表示请求已处理但结果不可知，
            # 所以带上真实状态码交给调用方判断，而不是笼统当成传输错误。
            raise XApiResponseError(
                resp.status_code,
                f"X API returned invalid JSON (status {resp.status_code}): {resp.text[:200]}",
            ) from exc
