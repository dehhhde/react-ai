# Configuration 配置参考

配置中心位于 [src/react_ai/config/settings.py](file:///d:/code/react-ai/src/react_ai/config/settings.py)，基于 `pydantic-settings`，同时读取 `.env` 文件与操作系统环境变量（环境变量优先）。全局单例：

```python
from react_ai.config.settings import settings   # AppSettings 实例
```

## AppSettings 结构

```text
AppSettings
├── env / app_name
├── llm:          LLMConfig
├── memory:       MemoryConfig
├── security:     SecurityConfig
├── observability: ObservabilityConfig
└── database:     DatabaseConfig
```

## LLMConfig（模型网关）

| 环境变量 (alias) | 字段 | 默认值 | 说明 |
|---|---|---|---|
| `LLM_DEFAULT_PROVIDER` | `default_provider` | `deepseek` | 默认厂商：`deepseek` / `anthropic` / 其他走 OpenAI 兼容 |
| `LLM_MODEL_NAME` | `model_name` | `deepseek-chat` | 模型名称 |
| `LLM_API_KEY` | `api_key` | `None` | API 密钥（勿硬编码，经 `.env` 注入） |
| `LLM_API_BASE` | `api_base` | `https://api.deepseek.com` | OpenAI 兼容网关地址 |
| `LLM_TIMEOUT` | `timeout_seconds` | `60.0` | 单次推理超时（秒） |
| `LLM_MAX_RETRIES` | `max_retries` | `3` | 失败重试次数 |

## MemoryConfig（记忆存储）

| 环境变量 | 字段 | 默认值 | 说明 |
|---|---|---|---|
| `MEMORY_TYPE` | `memory_type` | `in_memory` | 记忆后端类型：`in_memory` / `redis` |
| `REDIS_HOST` | `host` | `127.0.0.1` | Redis 主机 |
| `REDIS_PORT` | `port` | `6380` | Redis 端口 |
| `REDIS_PASSWORD` | `password` | `None` | Redis 密码 |
| `REDIS_DB` | `db` | `0` | Redis 库编号 |
| `REDIS_URL` | `redis_url` | `None` | 完整连接串（优先生效） |
| `SESSION_TTL` | `session_ttl_seconds` | `86400` | 会话缓存过期秒数 |

`resolved_redis_url` 属性按优先级拼装连接串：显式 `REDIS_URL` > 带密码 URL > 无密码 URL。

## SecurityConfig（安全护栏）

| 环境变量 | 字段 | 默认值 | 说明 |
|---|---|---|---|
| `ENABLE_GUARDRAILS` | `enable_guardrails` | `True` | 是否启用护栏 |
| `TOOL_TIMEOUT` | `max_tool_execution_timeout` | `30.0` | 工具执行最大超时（秒） |
| `SANDBOX_MODE` | `sandbox_mode` | `strict` | 沙箱模式 |

## ObservabilityConfig（可观测性）

| 环境变量 | 字段 | 默认值 | 说明 |
|---|---|---|---|
| `ENABLE_TRACING` | `enable_tracing` | `True` | 是否启用链路追踪 |
| `LOG_LEVEL` | `log_level` | `INFO` | 日志级别 |
| `OTEL_EXPORTER_OTLP_ENDPOINT` | `otlp_endpoint` | `None` | OTLP 导出端点 |

## DatabaseConfig（业务数据库）

| 环境变量 | 字段 | 默认值 |
|---|---|---|
| `DATABASE_URL` | `url` | `postgresql://root:123456@127.0.0.1:5433/ecommerce_aftersales` |
| `ASYNC_DATABASE_URL` | `async_url` | `postgresql+asyncpg://...`（同上异步驱动） |
| `POSTGRES_HOST` / `POSTGRES_PORT` | `host` / `port` | `127.0.0.1` / `5433` |
| `POSTGRES_USER` / `POSTGRES_PASSWORD` / `POSTGRES_DB` | `user` / `password` / `database` | `root` / `123456` / `ecommerce_aftersales` |

## 应用级

| 环境变量 | 字段 | 默认值 | 说明 |
|---|---|---|---|
| `APP_ENV` | `env` | `development` | 运行环境标识 |

## 最佳实践

1. **密钥治理**：生产环境通过操作系统环境变量或密钥管理服务注入，禁止提交 `.env`（已在 `.gitignore` 中忽略）。
2. **分层覆盖**：`.env` 提供本地默认值，CI/CD 与容器编排以环境变量覆盖。
3. **额外键忽略**：所有配置类均声明 `extra="ignore"`，`.env` 中的无关键不会导致启动失败。
