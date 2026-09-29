"""失败描述：一条错误信息必须能用来排障。

这个模块是从 `app/services/publish.py` 里提出来的，起因是 Phase 5B.1 的真机
冒烟：smoke-2 / smoke-4 报的是 `XApiTransportError: X API request could not be
sent:`——冒号后面什么都没有。httpx 连接失败时抛的 `ConnectError("")` 的 `str()`
本来就是空的，真正的原因（DNS / 代理 / TLS）在 `__cause__` 里。发布路径当时
已经会往下挖了，smoke 脚本自己拼字符串，于是同一个异常在两处读起来一个有用
一个没用。收敛成一个函数之后，这里钉住的就是它必须满足的那几条。
"""

from app.diagnostics import MAX_CAUSE_DEPTH, describe
from app.services.x_api import XApiTransportError


def test_a_message_is_used_as_is():
    assert describe(ValueError("boom")) == "boom"


def test_an_empty_message_degrades_to_the_class_name():
    """冒号后什么都没有是最坏的输出：它看起来像一句话，其实没有信息。"""
    assert describe(ValueError("")) == "ValueError"
    assert describe(ValueError("   ")) == "ValueError"


def test_an_empty_transport_error_reports_why_the_connection_failed():
    """这条是 smoke-2 / smoke-4 的原始形状，逐字重建。"""
    try:
        try:
            raise ConnectionError("x") from OSError("All connection attempts failed")
        except ConnectionError as exc:
            raise XApiTransportError(
                f"X API request could not be sent: {exc}", may_have_reached_x=False
            ) from exc
    except XApiTransportError as exc:
        text = describe(exc)

    assert "could not be sent" in text
    assert "All connection attempts failed" in text


def test_none_is_an_empty_string():
    """调用方常把它直接写进报告字段；返回 "None" 会让人以为有个叫 None 的异常。"""
    assert describe(None) == ""


def test_credentials_in_the_cause_chain_are_redacted(monkeypatch):
    """原因链是新引入的信息来源，脱敏必须跟着一起往下走。"""
    from app.config import settings

    monkeypatch.setattr(settings, "x_access_token_secret", "SUPER-SECRET-VALUE", raising=False)
    exc = ValueError("outer")
    exc.__cause__ = OSError("proxy auth failed with SUPER-SECRET-VALUE")

    text = describe(exc)

    assert "SUPER-SECRET-VALUE" not in text
    assert "***" in text


def test_a_repeated_message_is_not_printed_twice():
    """httpx 的包装经常让相邻两层说同一句话，重复只会把日志撑长。"""
    exc = ValueError("same")
    exc.__cause__ = OSError("same")

    assert describe(exc) == "same"


def test_the_chain_walk_is_bounded():
    """环状链（A 的 cause 是 B，B 的 cause 是 A）必须停下来，不能挂住调用方。"""
    first = ValueError("first")
    second = ValueError("second")
    first.__cause__ = second
    second.__cause__ = first

    parts = describe(first).split(" | ")

    assert parts == ["first", "second"]
    assert len(parts) <= MAX_CAUSE_DEPTH
