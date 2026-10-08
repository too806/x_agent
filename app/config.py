from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    app_name: str = "X AI Content Agent"
    database_url: str = "postgresql+psycopg://xagent:xagent@localhost:5432/xagent"

    # LangGraph Checkpointer 专用连接（libpq conninfo，非 SQLAlchemy URL）。
    # 留空则回退到 database_url（自动去掉 `+psycopg` 方言后缀）。
    # V1 与业务库指向同一个 PostgreSQL，但连接池与职责独立。
    langgraph_checkpoint_database_url: str = ""

    # 静态资源路径（相对项目根目录）
    persona_path: str = "config/persona.yaml"
    prompt_path: str = "prompts/generate.txt"
    # Phase 5A：Agent 的硬预算（max_steps / max_research_calls / ...）。
    # 与 persona 同样是配置而非代码常量：调预算不该改代码。
    agent_budget_path: str = "config/agent.yaml"
    # Phase 5B.2：Style Corpus 的采样与筛选阈值、Recipe 的分档切点。
    # 与预算同理——这些数会随着对样本的认知而调，调它不该改代码。
    style_config_path: str = "config/style.yaml"
    # 无人值守自动发布的开关与阈值。与上面三个的**关键区别**：这个文件
    # 每个 tick 重读一次，所以改它等于热杀开关（不用重启进程）。
    # 见 app/services/automation_config.py。
    automation_config_path: str = "config/automation.yaml"
    # 手工触发一次自动发布 tick 的令牌（`POST /api/admin/auto-publish/tick`）。
    #
    # **默认空 = 该端点拒绝一切请求。** 这是刻意的失败方向：控制台的其它端点
    # 都是只读的，只有这一个会在正式模式下真的发出一条帖子，而本仓库没有
    # 任何鉴权设施（没有登录、没有 session）。与其为它发明一套，
    # 不如让它默认不存在——不配令牌就用 CLI（那才是主机制）。
    #
    # 配了值之后，请求必须带 `X-Auto-Publish-Token` 且完全一致。
    auto_publish_tick_token: str = ""
    # Agent 各步 prompt 所在目录（planner / discover / evidence / angles / ...）。
    # 用目录而不是逐项列路径：每加一个 Tool 就要来 settings 里补一行，漏了会在
    # 运行到那一步时才炸。
    agent_prompt_dir: str = "prompts/agent"

    # LLM（OpenAI 兼容接口，base_url/model/api_key 均可经 .env 覆盖）
    llm_api_key: str = ""
    llm_base_url: str = "https://api.openai.com/v1"
    llm_model: str = "gpt-4o-mini"
    llm_timeout_seconds: float = 60.0

    # X API v2（search 使用 app-only Bearer token）
    x_api_base_url: str = "https://api.x.com"
    x_api_bearer_token: str = ""

    # X OAuth 1.0a 凭据（Publish 阶段使用）。
    # 四个值缺一不可：access token secret 同样参与 HMAC-SHA1 签名，
    # 只有 token 没有 secret 是签不出请求的。
    x_api_key: str = ""
    x_api_secret: str = ""
    x_access_token: str = ""
    x_access_token_secret: str = ""


settings = Settings()
