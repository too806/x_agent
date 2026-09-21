"""OAuth 1.0a 请求签名（HMAC-SHA1）。

X 的写接口（POST /2/tweets）要求 user context，也就是 OAuth 1.0a 用户签名；
app-only 的 Bearer token 只能读不能发。因此发布路径需要这一层。

本模块**只做签名，不发请求**（发请求在 app/services/x_api.py）。这样签名逻辑
可以用固定测试向量单独验证，不必碰网络，也不必持有真实凭据。

实现依据 RFC 5849 §3.4：
- 签名基串 = METHOD & percent_encode(URL) & percent_encode(排序后的参数串)
- 签名密钥 = percent_encode(consumer_secret) & percent_encode(token_secret)
- 参数串里 key/value 各自 percent_encode 后再拼接，整体再编码一次

注意：POST /2/tweets 的正文是 JSON，**不参与签名**——OAuth 1.0a 只把
form-encoded 正文计入参数集，JSON body 不在其中。这是最容易签错的地方。
"""
import base64
import hashlib
import hmac
import secrets
import time
from urllib.parse import quote

SIGNATURE_METHOD = "HMAC-SHA1"
OAUTH_VERSION = "1.0"


def percent_encode(value: str) -> str:
    """RFC 5849 §3.6 的百分号编码。

    quote 的 always-safe 集合恰好就是 RFC 3986 的 unreserved
    （字母、数字、`_` `.` `-` `~`），所以 safe="" 即可，不要传 "/"。
    """
    return quote(str(value), safe="")


def _parameter_string(params: dict[str, str]) -> str:
    """按编码后的 key / value 排序并拼接——排序必须在编码之后。"""
    encoded = sorted((percent_encode(k), percent_encode(v)) for k, v in params.items())
    return "&".join(f"{k}={v}" for k, v in encoded)


def build_signature_base_string(method: str, url: str, params: dict[str, str]) -> str:
    return "&".join(
        (method.upper(), percent_encode(url), percent_encode(_parameter_string(params)))
    )


def sign(
    method: str,
    url: str,
    params: dict[str, str],
    *,
    consumer_secret: str,
    token_secret: str,
) -> str:
    """返回 base64 编码的 HMAC-SHA1 签名。"""
    base_string = build_signature_base_string(method, url, params)
    signing_key = f"{percent_encode(consumer_secret)}&{percent_encode(token_secret)}"
    digest = hmac.new(
        signing_key.encode("utf-8"), base_string.encode("utf-8"), hashlib.sha1
    ).digest()
    return base64.b64encode(digest).decode("ascii")


def build_authorization_header(
    method: str,
    url: str,
    *,
    consumer_key: str,
    consumer_secret: str,
    token: str,
    token_secret: str,
    nonce: str | None = None,
    timestamp: int | None = None,
    extra_params: dict[str, str] | None = None,
) -> str:
    """构造 `Authorization: OAuth ...` 头。

    nonce / timestamp 可注入是为了让测试可复现——生产路径不要传，
    默认值保证每次请求的 nonce 都不同（重复 nonce 会被 X 判为重放）。
    """
    oauth_params = {
        "oauth_consumer_key": consumer_key,
        "oauth_nonce": nonce if nonce is not None else secrets.token_hex(16),
        "oauth_signature_method": SIGNATURE_METHOD,
        "oauth_timestamp": str(timestamp if timestamp is not None else int(time.time())),
        "oauth_token": token,
        "oauth_version": OAUTH_VERSION,
    }
    # 签名参数集 = oauth_* 参数 + 参与签名的额外参数（JSON body 不在此列）
    signing_params = {**oauth_params, **(extra_params or {})}
    signature = sign(
        method,
        url,
        signing_params,
        consumer_secret=consumer_secret,
        token_secret=token_secret,
    )
    header_params = {**oauth_params, "oauth_signature": signature}
    return "OAuth " + ", ".join(
        f'{percent_encode(k)}="{percent_encode(v)}"' for k, v in sorted(header_params.items())
    )
