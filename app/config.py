from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    app_name: str = "X AI Content Agent"
    database_url: str = "postgresql+psycopg://xagent:xagent@localhost:5432/xagent"

    # 静态资源路径（相对项目根目录）
    persona_path: str = "config/persona.yaml"
    prompt_path: str = "prompts/generate.txt"

    # LLM（OpenAI 兼容接口，base_url/model/api_key 均可经 .env 覆盖）
    llm_api_key: str = ""
    llm_base_url: str = "https://api.openai.com/v1"
    llm_model: str = "gpt-4o-mini"
    llm_timeout_seconds: float = 60.0

    # X API v2（search 使用 app-only Bearer token）
    x_api_base_url: str = "https://api.x.com"
    x_api_bearer_token: str = ""

    # X OAuth 1.0a 凭据（后续 Publish 阶段使用，本阶段未用）
    x_api_key: str = ""
    x_api_secret: str = ""
    x_access_token: str = ""


settings = Settings()
