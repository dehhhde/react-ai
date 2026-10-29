# Memory 分层记忆系统

模块：`react_ai.memory`。解决长轮次会话的上下文膨胀、跨实例共享与持久化审计三类问题，采用"记忆抽象 + 存储驱动"两层设计。

## 数据模型（memory/base.py）

文件：[memory/base.py](file:///d:/code/react-ai/src/react_ai/memory/base.py)

```python
class MemoryType(Enum):
    SESSION      # 会话记忆：当前对话窗口短期上下文
    WORKING      # 工作记忆：任务执行中间状态
    LONG_TERM    # 长期记忆：跨会话用户偏好、历史摘要
    TOOL_RESULT  # 工具结果缓存，避免重复调用

@dataclass
class MemoryItem:
    id, type, key, value
    session_id / user_id / tenant_id   # 三级数据隔离维度
    ttl_seconds                        # None 表示永不过期
    created_at / updated_at / access_count / metadata
    def is_expired(self) -> bool

@dataclass
class MemoryQuery:   # 多维度过滤：key/type/session_id/user_id/tenant_id/limit/include_expired
@dataclass
class MemoryResult:  # items + total_count + query_time_ms
```

## BaseMemory 基类

模板方法模式：公开 `write / read / delete / clear_session / clear_user / get_stats`（线程安全，RLock + 结构化审计日志 + 容量检查），子类实现四个抽象钩子：

| 抽象方法 | 职责 |
|---|---|
| `_do_write(item)` | 底层写入 |
| `_do_read(query)` | 底层查询 |
| `_do_delete(memory_id)` | 按 ID 删除 |
| `_evict()` | 超过 `max_items`（默认 10000）时的淘汰策略（LRU/LFU/TTL 清理） |

`read()` 默认过滤过期条目并累加 `access_count`。

## 短期记忆 WindowBufferMemory

文件：[memory/short_term.py](file:///d:/code/react-ai/src/react_ai/memory/short_term.py)。滑动窗口策略，仅保留最近 `k` 轮（即 `2k` 条消息）对话，控制 Token 膨胀：

```python
class WindowBufferMemory:
    def __init__(self, k: int = 10, store: BaseMessageStore | None = None): ...
    async def load_memory_variables(self, session_id: str) -> list[ChatMessage]
    async def save_context(self, session_id: str, human_input: str, ai_output: str) -> None
```

`store` 缺省为 `InMemoryMessageStore`，注入 Redis/Postgres Store 即可分布式化。

## 长期语义记忆 LongTermSemanticMemory

文件：[memory/long_term.py](file:///d:/code/react-ai/src/react_ai/memory/long_term.py)

```python
class LongTermSemanticMemory:
    def __init__(self, vector_index_name: str = "agent_memories"): ...
    async def search_relevant_facts(self, user_id: str, query: str, top_k: int = 5) -> list[str]
```

按用户输入跨会话召回相关事实/偏好画像。当前为接口占位实现，生产需对接向量数据库索引。

## 存储驱动 stores/

统一实现 `BaseMessageStore` 抽象（`get_messages` / `add_message` / `clear`，均为异步）：

| 驱动 | 文件 | 场景 |
|---|---|---|
| `InMemoryMessageStore` | [stores/in_memory.py](file:///d:/code/react-ai/src/react_ai/memory/stores/in_memory.py) | 单元测试与本地调试（单机易失） |
| `RedisMessageStore` | [stores/redis.py](file:///d:/code/react-ai/src/react_ai/memory/stores/redis.py) | 分布式会话缓存，`__init__(redis_url, ttl=86400)` 支持 TTL 自动过期，适合多实例横向扩展；底层复用 `memory/redis.py` 的 `RedisMemory`（`stores/redis.py` 中消息读写方法当前为骨架） |
| `PostgresMessageStore` | [stores/postgres.py](file:///d:/code/react-ai/src/react_ai/memory/stores/postgres.py) | 关系型持久化，全量会话审计归档 |

## 组合示例

```python
from react_ai.memory.short_term import WindowBufferMemory
from react_ai.memory.stores.redis import RedisMessageStore
from react_ai.config.settings import settings

memory = WindowBufferMemory(
    k=20,
    store=RedisMessageStore(
        redis_url=settings.memory.resolved_redis_url,
        ttl=settings.memory.session_ttl_seconds,
    ),
)

await memory.save_context("sess-1", "用户问题", "助手答复")
history = await memory.load_memory_variables("sess-1")   # 最近 40 条
```

## 选型建议

- 开发/测试：默认 `in_memory`（`MEMORY_TYPE=in_memory`）。
- 多副本生产：`MEMORY_TYPE=redis` + `SESSION_TTL` 控制会话生命周期。
- 审计合规：Postgres 归档全量消息，Redis 只承担热数据。
