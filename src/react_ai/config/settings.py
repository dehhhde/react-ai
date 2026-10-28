"""企业级集中配置中心 (支持环境变量分层覆盖)."""

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class LLMConfig(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        protected_namespaces=(),
        extra="ignore",
    )
    default_provider: str = Field(default="deepseek", alias="LLM_DEFAULT_PROVIDER")
    model_name: str = Field(default="deepseek-chat", alias="LLM_MODEL_NAME")
    api_key: str | None = Field(default=None, alias="LLM_API_KEY")
    api_base: str | None = Field(
        default="https://api.deepseek.com", alias="LLM_API_BASE"
    )
    timeout_seconds: float = Field(default=60.0, alias="LLM_TIMEOUT")
    max_retries: int = Field(default=3, alias="LLM_MAX_RETRIES")


class MemoryConfig(BaseSettings):
    memory_type: str = Field(default="in_memory", alias="MEMORY_TYPE")
    host: str = Field(default="127.0.0.1", alias="REDIS_HOST")
    port: int = Field(default=6380, alias="REDIS_PORT")
    password: str | None = Field(default=None, alias="REDIS_PASSWORD")
    db: int = Field(default=0, alias="REDIS_DB")
    redis_url: str | None = Field(default=None, alias="REDIS_URL")
    session_ttl_seconds: int = Field(default=86400, alias="SESSION_TTL")

    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    @property
    def resolved_redis_url(self) -> str:
        if self.redis_url:
            return self.redis_url
        if self.password:
            return f"redis://:{self.password}@{self.host}:{self.port}/{self.db}"
        return f"redis://{self.host}:{self.port}/{self.db}"


class SecurityConfig(BaseSettings):
    enable_guardrails: bool = Field(default=True, alias="ENABLE_GUARDRAILS")
    max_tool_execution_timeout: float = Field(default=30.0, alias="TOOL_TIMEOUT")
    sandbox_mode: str = Field(default="strict", alias="SANDBOX_MODE")


class ObservabilityConfig(BaseSettings):
    enable_tracing: bool = Field(default=True, alias="ENABLE_TRACING")
    log_level: str = Field(default="INFO", alias="LOG_LEVEL")
    otlp_endpoint: str | None = Field(default=None, alias="OTEL_EXPORTER_OTLP_ENDPOINT")


class DatabaseConfig(BaseSettings):
    url: str = Field(
        default="postgresql://root:123456@127.0.0.1:5433/ecommerce_aftersales",
        alias="DATABASE_URL",
    )
    async_url: str = Field(
        default="postgresql+asyncpg://root:123456@127.0.0.1:5433/ecommerce_aftersales",
        alias="ASYNC_DATABASE_URL",
    )
    host: str = Field(default="127.0.0.1", alias="POSTGRES_HOST")
    port: int = Field(default=5433, alias="POSTGRES_PORT")
    user: str = Field(default="root", alias="POSTGRES_USER")
    password: str = Field(default="123456", alias="POSTGRES_PASSWORD")
    database: str = Field(default="ecommerce_aftersales", alias="POSTGRES_DB")

    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )


class AppSettings(BaseSettings):
    env: str = Field(default="development", alias="APP_ENV")
    app_name: str = "react-ai"
    database: DatabaseConfig = Field(default_factory=DatabaseConfig)
    llm: LLMConfig = Field(default_factory=LLMConfig)
    memory: MemoryConfig = Field(default_factory=MemoryConfig)
    security: SecurityConfig = Field(default_factory=SecurityConfig)
    observability: ObservabilityConfig = Field(default_factory=ObservabilityConfig)

    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )


settings = AppSettings()
