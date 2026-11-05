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
    tool_result_ttl_seconds: int = Field(
        default=3600, alias="TOOL_RESULT_TTL"
    )  # 工具结果记忆 TTL（秒）
    working_ttl_seconds: int = Field(
        default=1800, alias="WORKING_TTL"
    )  # 工作记忆快照 TTL（秒）
    history_token_budget: int = Field(
        default=4000, alias="HISTORY_TOKEN_BUDGET"
    )  # 会话历史注入 LLM 前的 token 预算，<=0 表示不启用（差距分析 2.5）
    enforce_tenant_isolation: bool = Field(
        default=True, alias="ENFORCE_TENANT_ISOLATION"
    )  # 租户强制隔离闸（差距分析 2.6）：True 时禁止 tenant_id=None 的跨租户读写

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
    enable_hitl: bool = Field(default=True, alias="ENABLE_HITL")
    approval_timeout_seconds: float = Field(default=3600.0, alias="APPROVAL_TIMEOUT_SECONDS")
    enable_idempotency: bool = Field(default=True, alias="ENABLE_IDEMPOTENCY")
    idempotency_ttl_seconds: float = Field(default=86400.0, alias="IDEMPOTENCY_TTL_SECONDS")
    enable_result_cache: bool = Field(default=False, alias="ENABLE_RESULT_CACHE")
    enable_circuit_breaker: bool = Field(default=True, alias="ENABLE_CIRCUIT_BREAKER")
    enable_rate_limit: bool = Field(default=True, alias="ENABLE_RATE_LIMIT")


class ObservabilityConfig(BaseSettings):
    enable_tracing: bool = Field(default=True, alias="ENABLE_TRACING")
    log_level: str = Field(default="INFO", alias="LOG_LEVEL")
    otlp_endpoint: str | None = Field(default=None, alias="OTEL_EXPORTER_OTLP_ENDPOINT")
    # 审计存储后端：memory | postgres | elasticsearch
    audit_backend: str = Field(default="postgres", alias="AUDIT_BACKEND")
    # Elasticsearch 依赖服务留空占位，后续补充（如 http://127.0.0.1:9200）
    es_hosts: str | None = Field(default=None, alias="ES_HOSTS")
    es_index: str = Field(default="tool_call_audit", alias="ES_INDEX")
    es_api_key: str | None = Field(default=None, alias="ES_API_KEY")

    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )


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
