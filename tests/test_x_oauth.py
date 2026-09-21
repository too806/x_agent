"""OAuth 1.0a 签名单测。纯函数，不碰网络。"""
from app.services.x_oauth import (
    build_authorization_header,
    build_signature_base_string,
    percent_encode,
    sign,
)

# Twitter 官方文档里的 OAuth 1.0a 示例（form-encoded 正文参与签名的版本）。
# 用来钉住算法本身：只要基串构造、编码集合、签名密钥三处有一处写错，
# 这个签名就对不上。
EXAMPLE = {
    "consumer_key": "xvz1evFS4wEEPTGEFPHBog",
    "consumer_secret": "kAcSOqF21Fu85e7zjz7ZN2U4ZRhfV3WpwPAoE3Z7kBw",
    "token": "370773112-GmHxMAgYyLbNEtIKZeRNFsMKPR9EyMZeS9weJAEb",
    "token_secret": "LswwdoUaIvS8ltyTt5jkRh4J50vUPVVHtR2YPi5kE",
    "nonce": "kYjzVBB8Y0ZFabxSWbWovY3uYSQ2pTgmZeNu2VS4cg",
    "timestamp": 1318622958,
}
EXAMPLE_URL = "https://api.twitter.com/1.1/statuses/update.json"
EXAMPLE_BODY_PARAMS = {
    "status": "Hello Ladies + Gentlemen, a signed OAuth request!",
    "include_entities": "true",
}
EXAMPLE_SIGNATURE = "hCtSmYh+iHYCEqBWrE7C7hYmtUk="


def test_percent_encode_follows_rfc3986_unreserved_set():
    # unreserved 保持原样
    assert percent_encode("abcXYZ0189-._~") == "abcXYZ0189-._~"
    # 其余全部编码；空格必须是 %20 而不是 +
    assert percent_encode("Hello Ladies") == "Hello%20Ladies"
    assert percent_encode("+") == "%2B"
    assert percent_encode("!") == "%21"
    assert percent_encode("&") == "%26"
    assert percent_encode("=") == "%3D"
    assert percent_encode("/") == "%2F"
    assert percent_encode(":") == "%3A"


def test_signature_base_string_shape():
    base = build_signature_base_string(
        "POST", "https://api.example.com/1.1/x.json", {"b": "2", "a": "1"}
    )
    method, encoded_url, encoded_params = base.split("&")

    assert method == "POST"
    assert encoded_url == "https%3A%2F%2Fapi.example.com%2F1.1%2Fx.json"
    # 参数按编码后的 key 排序，整体再编码一次
    assert encoded_params == "a%3D1%26b%3D2"
    assert len(base.split("&")) == 3


def test_signature_base_string_is_encoded_exactly_once_more():
    """参数串整体还要再编码一次，所以 ']' 的 '%5D' 会变成 '%255D'。"""
    base = build_signature_base_string("GET", "https://e.com", {"a": "1", "]": "2"})

    assert base == "GET&https%3A%2F%2Fe.com&%255D%3D2%26a%3D1"


def test_sorting_happens_on_encoded_values_not_raw_ones():
    """排序必须在编码**之后**（RFC 5849 §3.4.1.3.2）。

    选 'z' 和 'é' 是因为两种顺序不同：按原始字符排 'z'(0x7A) < 'é'(0xE9)，
    按编码后排 'é'→'%C3%A9' 以 '%'(0x25) 开头，反而排在前面。
    用 ASCII 字符构造不出这个区别——它们的编码保留原有相对顺序。
    """
    base = build_signature_base_string("GET", "https://e.com", {"z": "1", "é": "2"})

    assert base.endswith("%25C3%25A9%3D2%26z%3D1")


def test_matches_known_oauth1_vector():
    """对官方示例的固定向量——算法级的正确性证据。"""
    params = {
        "oauth_consumer_key": EXAMPLE["consumer_key"],
        "oauth_nonce": EXAMPLE["nonce"],
        "oauth_signature_method": "HMAC-SHA1",
        "oauth_timestamp": str(EXAMPLE["timestamp"]),
        "oauth_token": EXAMPLE["token"],
        "oauth_version": "1.0",
        **EXAMPLE_BODY_PARAMS,
    }

    assert (
        sign(
            "POST",
            EXAMPLE_URL,
            params,
            consumer_secret=EXAMPLE["consumer_secret"],
            token_secret=EXAMPLE["token_secret"],
        )
        == EXAMPLE_SIGNATURE
    )


def test_signature_is_deterministic_for_fixed_inputs():
    params = {"oauth_consumer_key": "k", "oauth_nonce": "n", "oauth_timestamp": "1"}

    first = sign("POST", EXAMPLE_URL, params, consumer_secret="s", token_secret="t")
    second = sign("POST", EXAMPLE_URL, params, consumer_secret="s", token_secret="t")

    assert first == second


def test_signature_changes_with_each_input():
    params = {"oauth_consumer_key": "k", "oauth_nonce": "n", "oauth_timestamp": "1"}
    baseline = sign("POST", EXAMPLE_URL, params, consumer_secret="s", token_secret="t")

    assert sign("GET", EXAMPLE_URL, params, consumer_secret="s", token_secret="t") != baseline
    assert (
        sign("POST", EXAMPLE_URL, params, consumer_secret="s2", token_secret="t") != baseline
    )
    assert (
        sign("POST", EXAMPLE_URL, params, consumer_secret="s", token_secret="t2") != baseline
    )
    assert (
        sign("POST", EXAMPLE_URL, {"oauth_nonce": "x"}, consumer_secret="s", token_secret="t")
        != baseline
    )


def test_json_body_is_not_part_of_the_signature():
    """钉住最容易签错的地方：POST /2/tweets 的 JSON 正文不参与签名。

    OAuth 1.0a 只把 form-encoded 正文计入参数集。把 JSON 正文塞进去会导致
    X 端校验失败（401），而且失败信息不会告诉你是签名错在哪。
    """
    header = build_authorization_header(
        "POST",
        "https://api.x.com/2/tweets",
        consumer_key=EXAMPLE["consumer_key"],
        consumer_secret=EXAMPLE["consumer_secret"],
        token=EXAMPLE["token"],
        token_secret=EXAMPLE["token_secret"],
        nonce=EXAMPLE["nonce"],
        timestamp=EXAMPLE["timestamp"],
    )
    without_body = build_authorization_header(
        "POST",
        "https://api.x.com/2/tweets",
        consumer_key=EXAMPLE["consumer_key"],
        consumer_secret=EXAMPLE["consumer_secret"],
        token=EXAMPLE["token"],
        token_secret=EXAMPLE["token_secret"],
        nonce=EXAMPLE["nonce"],
        timestamp=EXAMPLE["timestamp"],
        extra_params={"text": "AI coding agents ship faster."},
    )

    # 传了 extra_params 就会改变签名——这正是"不要传正文"的原因
    assert "oauth_signature=" in header
    assert header != without_body


def test_authorization_header_shape():
    header = build_authorization_header(
        "POST",
        "https://api.x.com/2/tweets",
        consumer_key="ck",
        consumer_secret="cs",
        token="tk",
        token_secret="ts",
        nonce="fixed-nonce",
        timestamp=1700000000,
    )

    assert header.startswith("OAuth ")
    assert 'oauth_consumer_key="ck"' in header
    assert 'oauth_nonce="fixed-nonce"' in header
    assert 'oauth_timestamp="1700000000"' in header
    assert 'oauth_signature_method="HMAC-SHA1"' in header
    assert 'oauth_version="1.0"' in header
    assert 'oauth_token="tk"' in header
    assert "oauth_signature=" in header
    # 密钥绝不出现在头里
    assert "cs" not in header.replace("ck", "")
    assert "ts" not in header


def test_header_nonce_and_timestamp_vary_by_default():
    def build():
        return build_authorization_header(
            "POST",
            "https://api.x.com/2/tweets",
            consumer_key="ck",
            consumer_secret="cs",
            token="tk",
            token_secret="ts",
        )

    # 默认 nonce 每次都要不同：重复 nonce 会被 X 判为重放
    assert build() != build()
