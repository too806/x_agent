"""凭据脱敏。

错误信息会被写进数据库（agent_run.error / published_post.last_error）和日志，
这些地方都不允许出现凭据。调用 X 的代码路径不止一处（graph 节点、发布服务），
所以脱敏放在共享模块里，而不是留在 HTTP 层。
"""
from app.config import settings


def redact(message: str) -> str:
    """抹掉错误信息中可能出现的凭据。"""
    for secret in (
        settings.x_api_bearer_token,
        settings.llm_api_key,
        settings.x_api_key,
        settings.x_api_secret,
        settings.x_access_token,
        settings.x_access_token_secret,
    ):
        if secret:
            message = message.replace(secret, "***")
    return message
