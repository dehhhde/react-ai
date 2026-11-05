# 记忆模块企业级差距分析

> 适用对象：[src/react_ai/memory/](file:///d:/code/react-ai/src/react_ai/memory/) 模块及其在项目中的实际使用情况。
> 本文档回答：**当前 memory 模块到了什么水准？距离企业级生产记忆系统还差什么、怎么补？**
> 更新时间：2026-11-05（已按代码现状核对各项落地状态）

---

## 1. 当前架构速览

```text
   API 请求                    核心执行链                    memory/ 模块
┌──────────────┐   ┌──────────────────────────┐   ┌─────────────────────────┐
│ ChatRequest  │   │ ReActAgent.stream/_loop   │   │ BaseMemory (抽象齐全)    │
│ .session_id ─┼──►│ RunContext(tenant/user/  │   │ InMemory / Redis         │
│ +身份三元组  │   │ session) 三元组          │   │ WindowBufferMemory       │
└──────────────┘   │ 入口 load_session ───────┼──►│ SessionKey 身份绑定读写   │
                   │ 出口 save_turn  ─────────┼──►│ PostgresMessageStore    │
                   └──────────────────────────┘   │ LongTermSemanticMemory   │
                     ✅ 引擎已接入记忆(2.1)         └─────────────────────────┘
                        ✅ 会话按三元组隔离(2.2)
```

### 已实现能力清单

| 能力 | 实现位置 | 说明 |
|---|---|---|
| 统一记忆模型 | [memory/base.py#L13-L39](file:///d:/code/react-ai/src/react_ai/memory/base.py#L13-L39) `MemoryItem` | `MemoryType` 区分会话/工作/长期/工具结果；含 `session_id/user_id/tenant_id/ttl_seconds/metadata` |
| 抽象层与查询 | [memory/base.py#L98-L128](file:///d:/code/react-ai/src/react_ai/memory/base.py#L98-L128) `BaseMemory`/`MemoryQuery` | 按 session/user/tenant/key/type 多维过滤，统一读写、淘汰、清理接口 |
| 内存实现 | [memory/in_memory.py#L14-L101](file:///d:/code/react-ai/src/react_ai/memory/in_memory.py#L14-L101) `InMemoryMemory` | 线程安全、多维索引、过期过滤、会话隔离、查询截断 |
| 容量淘汰 | [memory/in_memory.py#L110-L157](file:///d:/code/react-ai/src/react_ai/memory/in_memory.py#L110-L157) | 优先清过期项，再按 LRU/LFU/FIFO 淘汰至 90% 容量 |
| Redis 实现 | [memory/redis.py#L248-L293](file:///d:/code/react-ai/src/react_ai/memory/redis.py#L248-L293) `RedisMemory` | ZSet 多维索引（all/session/user/tenant/type/key）、原生 PX TTL |
| 短期滑动窗口 | [memory/short_term.py](file:///d:/code/react-ai/src/react_ai/memory/short_term.py) `WindowBufferMemory` | 基于 `BaseMessageStore` 加载最近 `2*k` 条消息，再按 token 预算从最旧丢弃（2.5） |
| 消息存储抽象 | [memory/stores/](file:///d:/code/react-ai/src/react_ai/memory/stores/) | `InMemoryMessageStore` / `RedisMessageStore` / `PostgresMessageStore` 三实现 |
| 会话身份三元组 | [memory/base.py](file:///d:/code/react-ai/src/react_ai/memory/base.py) `SessionKey` | `(tenant_id, user_id, session_id)` 联合定位，禁止裸 session_id；`load_session/save_turn` 按三元组读写并附加 TTL |

**结论：抽象层设计是企业级的（多维隔离、TTL、淘汰策略都有）。2.1（引擎接入）、2.2（session 身份三元组绑定）、2.4（配置驱动装配工厂）、2.5（token 预算控制）、2.6（租户强制隔离闸）、2.7（自动沉淀管道）与 2.8（记忆路径可观测）已落地，多轮对话成立且跨 tenant/user 隔离、后端切换可运维、上下文膨胀有预算红线、跨租户读写有硬闸、记忆读写与召回全链路可追踪可度量；唯一剩余项是 2.3 的验收测试待执行。**

---

## 2. 企业级差距分析（按重要性排序）

### 2.1 与核心引擎完全脱节 —— memory 模块是孤岛 ⚠️ P0 ✅ 已完成

**现状**：grep 全项目，`react_ai.memory` 仅被包自身 `__init__.py` 与 [tests/unit/test_memory.py](file:///d:/code/react-ai/tests/unit/test_memory.py) 引用；[core/engine.py](file:///d:/code/react-ai/src/react_ai/core/engine.py)、[api/routes/agent.py](file:///d:/code/react-ai/api/routes/agent.py)、examples 均无任何 import。`ReActAgent.stream()` 每次请求从空 `state.messages` 开始，执行结束后也不回写任何记忆。

**风险**：
1. 记忆系统形同虚设，框架宣称的"分层记忆治理"（[Home.md](file:///d:/code/react-ai/wiki/Home.md)）在生产链路上不存在；
2. 所有对话都是单轮，Agent 无法引用同会话早前信息；
3. 已投入的 Redis/PG 存储实现永远不被执行，测试维护成本白付。

**企业级做法**：在 `ReActAgent` 注入 memory 依赖，执行链两端接入：

```python
class ReActAgent:
    def __init__(self, ..., memory: BaseMemory | None = None):
        self.memory = memory

    async def stream(self, query, ctx):
        # 入口：按 session_id 恢复历史
        if self.memory:
            state.messages = await self.memory.load_session(ctx.session_id)
        state.messages.append(ChatMessage(role=USER, content=query))
        ...  # ReAct 循环
        # 出口：回写本轮 user/assistant 消息
        if self.memory:
            await self.memory.save_turn(ctx.session_id, state.messages)
```

**验收标准**：同一 `session_id` 的第二次请求，LLM 收到的 messages 包含首轮对话；`state.messages` 来源可追溯到 memory 模块；核心引擎有 memory 依赖注入点。

---

### 2.2 session_id 形同虚设 —— 多轮对话不成立 ⚠️ P0 ✅ 已完成

**现状**：[api/routes/agent.py#L80-L102](file:///d:/code/react-ai/api/routes/agent.py#L80-L102) 中 `ChatRequest.session_id` 传入 `RunContext` 后仅作为事件元数据透传，没有任何历史加载/保存动作；缺省值 `"default-session"` 使所有未传 session 的请求共享同一标识。

**风险**：
1. 用户以为在多轮，实际每轮都是全新对话，体验与业务正确性双重失败；
2. `default-session` 全局共享，一旦接入记忆就是**跨用户数据串扰**（A 用户的历史出现在 B 用户上下文），属于合规事故面。

**企业级做法**：
1. session 必须与身份绑定：`session_key = (tenant_id, user_id, session_id)` 三元组，禁止裸 `session_id` 查询；
2. 生产环境 API 层强制要求 `session_id`（或网关签发），去掉 `"default-session"` 兜底；
3. 会话 TTL 生效（见 2.4），过期会话自动开新会话。

**验收标准**：不同 tenant/user 下相同 `session_id` 字符串互不可见；未授权访问他人会话返回 404/403；匿名请求策略明确（拒绝或独立隔离命名空间）。

**落地情况（2026-11-04）**：
1. 新增 `SessionKey(tenant_id, user_id, session_id)` 冻结三元组（[memory/base.py](file:///d:/code/react-ai/src/react_ai/memory/base.py) `SessionKey`），`__post_init__` 强制三维度非空，`SessionKey.resolve()` 在 `session_id` 缺失时抛 `ValueError`，`tenant/user` 缺失时落入独立匿名命名空间 `anonymous::anonymous::<session>`，禁止裸 `session_id` 查询；
2. `BaseMemory` 新增身份绑定的 `load_session(key)` / `save_turn(key, messages)`，查询与写入均经三元组过滤，`load_session` 按时间正序返回最近 N 条，`save_turn` 仅持久化 user/assistant 纯文本消息并打上 `session_ttl_seconds` TTL；
3. API 层 `ChatRequest.session_id` 改为必填（`Field(..., min_length=1)`），去掉 `default-session` 兜底，新增 `user_id/tenant_id` 字段，`_build_context()` 透传到 `RunContext`；
4. 新增 `GET /api/v1/agent/sessions/{session_id}/messages`：按三元组查询，他人身份因三元组不匹配查不到数据统一返回 404（不泄露会话是否存在）；
5. `RunContext` 增加 `tenant_id` 字段，`RunCheckpoint` 同步增加 `tenant_id`，`resume()` 与 `_save_checkpoint()` 均透传，保证审批挂起后恢复时的身份一致；
6. 验收测试 [tests/unit/test_session_identity.py](file:///d:/code/react-ai/tests/unit/test_session_identity.py) 覆盖三元组校验、跨 tenant/user 隔离、匿名命名空间隔离、TTL 过期、API 422/404、端到端多轮对话；
7. 匿名请求策略：tenant/user 缺失时落入独立匿名命名空间，不与任何已认证身份共享（文档与代码均已落地）。

---

### 2.3 长期语义记忆是占位假实现 🎭 P0 ✅ 已实现 ⏳ 待测试

**现状**：[memory/long_term.py#L6-L14](file:///d:/code/react-ai/src/react_ai/memory/long_term.py#L6-L14) `LongTermSemanticMemory.search_relevant_facts` 不接任何向量库，硬编码返回一条示例文本 `"用户历史偏好记录: 偏好使用 Python 异步风格代码 (相关度 0.94)"`，`vector_index_name` 参数从未被消费。

**风险**：任何调用方拿到的"用户长期偏好"是同一句假话——比没有记忆更糟，会系统性污染决策且极难从输出中发现。

**企业级做法**：二选一，不允许保持现状：
1. **真实现**：接入向量库（pgvector 复用现有 PG / Milvus / Qdrant），写入侧在会话结束时抽取事实并 embedding，检索侧 `search_relevant_facts` 走 ANN top-k；
2. **显式未实现**：方法抛 `NotImplementedError` 并在 Wiki 标注，从 `__init__.py` 公开导出中移除，防止误用。

推荐 1，且与 `tools/builtin/retrieval.py` 的检索基建复用（Prompts 2.8 动态少样本也依赖同一能力）。

**验收标准**：同一 user_id 写入事实 A 后，语义相近 query 可召回 A；无关用户不可召回；`vector_index_name` 真实生效。

**落地情况（2026-11-04，⏳ 待测试）**：
1. 新增向量检索基建子包 [memory/vector/](file:///d:/code/react-ai/src/react_ai/memory/vector/)：`BaseEmbeddingProvider`/`BaseVectorStore` 抽象 + `VectorRecord`/`SearchHit` 数据模型，检索接口内置 user_id/tenant_id 身份过滤参数；
2. Embedding 实现：[vector/embedding.py](file:///d:/code/react-ai/src/react_ai/memory/vector/embedding.py) `OpenAIEmbeddingProvider`（OpenAI 兼容 `/embeddings`，text-embedding-3 系列支持 Matryoshka 维度，可注入 `httpx.MockTransport` 隔离网络）与 `HashingEmbeddingProvider`（确定性特征哈希伪向量；零 Key 时前者自动降级为后者并告警，与 `OpenAIProvider` 离线演示约定同款）；
3. 向量存储实现：[vector/pgvector.py](file:///d:/code/react-ai/src/react_ai/memory/vector/pgvector.py) `PgVectorStore`（pgvector 复用现有 PG，索引名即表名、自动建扩展建表、余弦距离 `<=>` ANN 检索、身份过滤在 SQL 层强制、同步 psycopg 经 `asyncio.to_thread` 卸载、表名白名单防注入）与 [vector/in_memory.py](file:///d:/code/react-ai/src/react_ai/memory/vector/in_memory.py) `InMemoryVectorStore`（余弦相似度暴力检索，身份过滤先于打分）；
4. [memory/long_term.py](file:///d:/code/react-ai/src/react_ai/memory/long_term.py) 重写为真实现：`add_fact`（事实 → embedding → upsert，按 user/tenant 打身份标签）、`search_relevant_facts`（query embedding → ANN top-k，签名兼容原接口，新增 `min_score` 过滤）、`search_relevant`（返回含得分命中）、`forget_user`（被遗忘权删除）；`vector_index_name` 真实传递到存储层；硬编码假文本已删除；
5. 缺省装配零外部依赖即用（Hashing + InMemory），生产注入 `OpenAIEmbeddingProvider` + `PgVectorStore`；配置驱动装配属 2.4，本项未越界。
6. ⏳ **待测试**：验收用例未编写——写召闭环（事实 A 写入后语义相近 query 召回）、跨 user/tenant 不可见、`vector_index_name` 生效、PgVectorStore 真实 PG + pgvector 环境集成。

---

### 2.4 配置驱动装配缺失 —— memory_type 是死配置 📦 P1 ✅ 已完成

**现状**：[config/settings.py#L24-L31](file:///d:/code/react-ai/src/react_ai/config/settings.py#L24-L31) 定义了 `MEMORY_TYPE`、`SESSION_TTL`，`MemoryConfig.resolved_redis_url`（[L38-L43](file:///d:/code/react-ai/src/react_ai/config/settings.py#L38-L43)）实现了 URL 拼装——但 grep 全项目，这三个字段/属性**只被 `scripts/` 下的开发脚本引用**，src 与 api 无任何读取点。没有 `create_memory_from_settings()` 工厂，Redis/InMemory 切换无从配置。这与 Prompts 2.10"无外部存储装配"是同一类缺口，且审计存储已有正确示范（`build_audit_store_from_settings`，[api/routes/agent.py#L61](file:///d:/code/react-ai/api/routes/agent.py#L61)）。

**风险**：环境变量 `MEMORY_TYPE=redis` 设了也没用，运维误以为已启用分布式记忆；单进程重启后所有会话记忆丢失。

**企业级做法**：仿照审计存储工厂：

```python
def create_memory_from_settings() -> BaseMemory:
    if settings.memory.memory_type == "redis":
        return RedisMemory(url=settings.memory.resolved_redis_url,
                           default_ttl=settings.memory.session_ttl_seconds)
    return InMemoryMemory()
```

依赖服务不可用时按审计存储同款策略降级内存并告警，不阻断服务启动。

**验收标准**：设置 `MEMORY_TYPE=redis` 后无需改代码即切换到 Redis 实现；`SESSION_TTL` 真实控制会话过期；未配置时行为与现在一致。

**落地情况（2026-11-05）**：
1. 新增装配工厂 [memory/factory.py](file:///d:/code/react-ai/src/react_ai/memory/factory.py) `create_memory_from_settings()`：按 `settings.memory.memory_type` 构建后端——`redis` 时用 `resolved_redis_url`（REDIS_URL 或 HOST/PORT/PASSWORD/DB 拼装）构造 `RedisMemory` 并 `is_connected()` 探活；`in_memory` 或未知取值构建 `InMemoryMemory`；两种实现的 `session_ttl_seconds` 均真实取自 `SESSION_TTL`；
2. 降级策略与审计工厂同款：redis 包缺失、连接异常、探活失败均告警降级内存，不阻断服务启动；
3. [api/routes/agent.py](file:///d:/code/react-ai/api/routes/agent.py) `get_agent()` 改为调用工厂，删除写死的 `InMemoryMemory` 构造；工厂函数经 `memory/__init__.py` 公开导出；
4. 验收测试 [tests/unit/test_memory_factory.py](file:///d:/code/react-ai/tests/unit/test_memory_factory.py) 7 项（本机无 redis 包/服务，用假模块注入，零真实连接）：默认内存装配、TTL 透传、`MEMORY_TYPE=redis` 切换、URL 拼装、探活失败降级、包缺失降级、未知取值降级、`get_agent()` 确实经由工厂装配。

---

### 2.5 无 token 预算控制 —— 滑动窗口只数条数 📏 P1 ✅ 已完成

**现状**：[memory/short_term.py#L9-L28](file:///d:/code/react-ai/src/react_ai/memory/short_term.py#L9-L28) 按消息条数（`2*k`）截断历史。工具 observation 可能是数千 token 的长文本，条数限制拦不住上下文爆炸。项目已有 [utils/token_counter.py](file:///d:/code/react-ai/src/react_ai/utils/token_counter.py)（Prompts 2.7 已接入渲染侧），memory 侧未复用。

**风险**：长会话下 prompt 超限报错或静默截断关键信息；token 成本不可控。

**企业级做法**：窗口加载后按 `estimate_messages_tokens` 从最旧消息开始丢弃直到预算内；或引入摘要压缩（旧对话压缩为一条 summary 消息，摘要 LLM 调用走网关降级链）。

**验收标准**：任意历史长度下注入 LLM 的历史部分 token 不超过配置预算；提供预算与截断事件指标。

**落地情况（2026-11-05）**：
1. [utils/token_counter.py](file:///d:/code/react-ai/src/react_ai/utils/token_counter.py) 新增 `trim_messages_to_token_budget(messages, budget)`：从最旧消息开始丢弃直到总量进入预算，至少保留最新一条（空历史比超限更糟），`budget <= 0` 视为不启用；
2. [config/settings.py](file:///d:/code/react-ai/src/react_ai/config/settings.py) `MemoryConfig` 新增 `history_token_budget`（环境变量 `HISTORY_TOKEN_BUDGET`，默认 4000）；
3. [memory/base.py](file:///d:/code/react-ai/src/react_ai/memory/base.py) `BaseMemory.load_session` 在条数截断后追加 token 预算裁剪：预算优先级为调用级 `token_budget` > 实例级 `history_token_budget`，两者均未配置时行为与改造前一致；引擎入口 `_load_memory_history` 经此路径自动生效，无需改动引擎；
4. 截断事件指标：命中裁剪时输出结构化告警 `memory_token_budget_truncated`（含预算/丢弃条数/保留条数/保留 token 数），实例累计计数经 `get_stats()` 暴露（`token_truncated_events` / `token_truncated_messages` / `history_token_budget`）；
5. [memory/short_term.py](file:///d:/code/react-ai/src/react_ai/memory/short_term.py) `WindowBufferMemory` 同步接入两级裁剪（先条数 `2*k`，后 token 预算）；`InMemoryMemory`/`RedisMemory` 构造新增 `history_token_budget` 透传；
6. [memory/factory.py](file:///d:/code/react-ai/src/react_ai/memory/factory.py) 装配工厂将 `HISTORY_TOKEN_BUDGET` 真实透传到两种后端实例；
7. 验收测试 [tests/unit/test_memory_token_budget.py](file:///d:/code/react-ai/tests/unit/test_memory_token_budget.py) 15 项（纯内存实现，零外部依赖）：裁剪函数语义、任意历史长度下 `load_session` 结果不超预算且保留最新、预算未配置向后兼容、调用级预算覆盖实例级、截断事件计数、工厂透传（redis 用假模块注入）。
8. 说明：摘要压缩（旧对话压缩为 summary 消息）为可选增强，未纳入本项范围。

---

### 2.6 租户字段有、隔离逻辑无 🔒 P1 ✅ 已完成

**现状**：`MemoryItem.tenant_id` 与 `MemoryQuery` 的 tenant 过滤在存储层实现了（[memory/redis.py#L248-L293](file:///d:/code/react-ai/src/react_ai/memory/redis.py#L248-L293) 维护 tenant 索引），但没有任何调用方被强制传入 tenant——查询不传 `tenant_id` 即跨租户全量可见。API 层没有租户身份入口（参见 API-Service-Gap-Analysis 2.4）。

**风险**：隔离能力"存在但可绕过"，比没有更危险——审计时容易误判已隔离。

**企业级做法**：在服务层（API 依赖注入）解析租户身份，memory 查询封装层强制注入 `tenant_id`，禁止 None；存储层对缺失 tenant 的跨租户查询直接拒绝。

**验收标准**：不带 tenant_id 的查询在框架层抛错或被强制限定到请求租户；测试覆盖跨租户不可见断言。

**落地情况（2026-11-05）**：
1. [memory/base.py](file:///d:/code/react-ai/src/react_ai/memory/base.py) `BaseMemory` 新增 `enforce_tenant_isolation: bool = True` 开关，生产默认启用；
2. **`write` 入口硬闸**：开关启用时 `item.tenant_id` 为空直接抛 `ValueError`，禁止写入无租户条目；
3. **`read` 入口硬闸**：开关启用时 `query.tenant_id is None` 直接抛 `ValueError`，禁止跨租户无界查询；
4. **`clear_session/clear_user` 强制 tenant**：两个清理方法新增 `tenant_id` 参数（开关启用时必填），内部改用 `self.read()` 走统一闸，不再直接 `_do_read`；
5. SessionKey 路径（2.2 已落地的 `load_session/save_turn/save_working_state/save_tool_result`）写入的条目天然带 `SessionKey.tenant_id`，查询也天然带 tenant 过滤——**不受新闸影响、零改动**；
6. [config/settings.py](file:///d:/code/react-ai/src/react_ai/config/settings.py) `MemoryConfig` 新增 `ENFORCE_TENANT_ISOLATION` 环境变量，[factory.py](file:///d:/code/react-ai/src/react_ai/memory/factory.py) 装配工厂真实透传到 `InMemoryMemory` / `RedisMemory`；
7. 两个存储实现（`InMemoryMemory` / `RedisMemory`）构造新增 `enforce_tenant_isolation` 参数透传给父类；`get_stats()` 暴露 `enforce_tenant_isolation` 字段；
8. 验收测试 [tests/unit/test_memory_tenant_isolation.py](file:///d:/code/react-ai/tests/unit/test_memory_tenant_isolation.py) 16 项（纯内存实现，零外部依赖）：write/read/clear 入口闸、跨租户同 session_id 互不可见、SessionKey 路径天然过闸、开关关闭向后兼容、工厂透传；
9. 修正 `test_memory_types_integration.py` 中 4 处裸 tenant 的 `memory.read(MemoryQuery(type=...))`，补上 `tenant_id="t1"` 与 RunContext 对齐；
10. 说明：开关关闭（`ENFORCE_TENANT_ISOLATION=false`）保留向后兼容，用于开发/调试场景；生产默认 True 强制隔离。

---

### 2.7 只读不写 —— 无自动沉淀管道 🗂 P2 ✅ 已完成

**现状**：模块提供了完整写入 API，但没有任何"对话结束 → 写会话记忆""工具结果 → 写 working memory""会话摘要 → 写长期记忆"的自动管道；全部依赖业务方手动调用（而业务方目前根本不知道它存在）。

**企业级做法**：利用已有生命周期钩子（[core/lifecycle.py](file:///d:/code/react-ai/src/react_ai/core/lifecycle.py)）注册默认记忆钩子：run 成功后写 user/assistant 消息，工具结果按 `MemoryType.TOOL_RESULT` + TTL 写 working 记忆，会话空闲时触发事实抽取。

**验收标准**：开启钩子后业务代码零改动即获得多轮记忆；工具大结果不再全量驻留上下文而是按需召回。

**落地情况（2026-11-04）**：
1. `BaseMemory` 新增工作记忆 `save_working_state/load_working_state`（按 run_id 确定性 ID 原地更新）与工具结果记忆 `save_tool_result/load_tool_result`（键 = `tool_result:{工具名}:{参数sha256指纹}`，参数顺序无关命中同键）；
2. 引擎接入三类自动录入时机：**TOOL_RESULT** 在 `_run_tool_calls` 成功分支录入（仅 ok 且非幂等重放，失败结果不入库），TTL 由 `settings.memory.tool_result_ttl_seconds`（默认 3600s）控制；**WORKING** 在 `_loop` 每步工具调用完成后、最终答复前、Token 预算退出前三处录入快照（step/status/last_action/last_observation 截断 200 字），TTL 由 `settings.memory.working_ttl_seconds`（默认 1800s）控制；**LONG_TERM** 在 run 正常结束出口由 `_sediment_long_term_facts` 用确定性规则抽取用户显式声明的姓名/身份/偏好/要求事实写入 `LongTermSemanticMemory`（写入前经 `PromptInjectionGuardrail` 闸门，命中即丢弃禁止入库；同文本去重），入口由 `_recall_long_term_facts` 按 user_id 召回 top-3 渲染为独立 system 参考块注入；
3. 隔离与降级：三类录入全部走 `SessionKey` 三元组打标；长期记忆仅当显式注入 `long_term` 且携带 `user_id` 时启用（匿名请求不沉淀）；任何录入/召回故障仅告警不阻断主流程；未注入依赖时行为与改造前一致；
4. 验收测试：[tests/unit/test_memory_types_integration.py](file:///d:/code/react-ai/tests/unit/test_memory_types_integration.py) 12 项覆盖录入时机、身份隔离、参数指纹召回、注入事实拒入、去重、失败 run 零沉淀、向后兼容。
5. 说明：事实抽取为确定性规则（非 LLM 摘要），会话摘要级长期沉淀与 TOOL_RESULT 自动重放短路（避免重复调用）仍属后续增强项。

---

### 2.8 记忆路径零可观测 📊 P2 ✅ 已完成

**现状**：memory 模块内无 tracer/metrics 引用；`/spans`、`/metrics` 出口（[api/routes/observability.py#L12-L41](file:///d:/code/react-ai/api/routes/observability.py#L12-L41)）里看不到任何记忆相关 span 或指标。

**风险**：线上"记忆没生效/召回错内容"类问题无从定位；容量、命中率、淘汰频率无数据。

**企业级做法**：`load_session/save_turn/search` 包 `tracer.start_span("memory.*")`；上报命中率、淘汰计数、存储后端健康度到 `MetricsCollector`。

**验收标准**：链路中可见 `memory.load` / `memory.save` span；`/metrics` 输出含 `memory_*` 指标。

**落地情况（2026-11-05）**：
1. **链路追踪**：`BaseMemory` 全部入口接入 span —— `memory.load`（`load_session`）、`memory.save`（`save_turn`）、`memory.read`、`memory.write`、`memory.delete`、`memory.clear_session`、`memory.clear_user`；`LongTermSemanticMemory` 接入 `memory.save`（`add_fact`）、`memory.search`（`_search`）、`memory.delete`（`forget_user`）；
2. **span 属性只带身份与统计维度**（backend / tenant_id / user_id / session_id / type / 条数 / top_k），**不含消息正文**，避免敏感内容进入链路；
3. **指标采集**：[metrics.py](file:///d:/code/react-ai/src/react_ai/observability/metrics.py) 新增 `record_memory_op()` 与 `record_memory_eviction()`，覆盖操作计数、耗时分布、条目数、命中/未命中、淘汰数、后端健康度；
4. **/metrics 输出**：`react_ai_memory_ops_total`（op/backend/tenant/result）、`react_ai_memory_op_duration_ms_count|_sum`、`react_ai_memory_items_total`、`react_ai_memory_evictions_total`、`react_ai_memory_backend_up`；
5. **snapshot 输出**：`memory_ops`、`memory_hit_rate`（按 op+backend 聚合 hit/miss 与命中率）、`memory_evictions`、`memory_backend_health`；
6. **健康度语义**：后端最近一次操作异常记 0，下一次成功自动回升为 1，无需外部探针；
7. **指标层降级**：指标上报与 span 采集异常均被吞掉，不影响记忆读写主流程；
8. 验收测试 [tests/unit/test_memory_observability.py](file:///d:/code/react-ai/tests/unit/test_memory_observability.py) 12 项（纯内存实现，零外部依赖）：span 存在性与 status、span 不泄漏正文、租户闸失败产生 error span、操作计数、miss→hit 命中率、健康度下降与恢复、淘汰计数、长期记忆检索命中率、`/metrics` 与 `/metrics/snapshot` HTTP 出口。

---

## 3. 实施优先级路线

| 级别 | 事项 | 排序依据 |
|---|---|---|
| **P0（阻塞上线）** | 2.1 引擎接入 → 2.2 session 身份绑定 → 2.3 长期记忆真实现/显式下线 | 不做则记忆模块白建、多轮对话不成立、假召回污染决策 |
| **P1（可运维 / 安全）** | 2.4 配置装配工厂 → 2.5 token 预算 → 2.6 租户强制隔离 | 死配置误导运维；上下文爆炸是成本与稳定性红线；隔离是合规红线 |
| **P2（体验 / 效率）** | 2.7 自动沉淀管道 → 2.8 记忆可观测 | 依赖 P0/P1 落地后收益更大 |

> **落地核对（2026-11-05）**：2.1 已完成（引擎注入 `memory` 依赖、`stream/resume` 两端接入 `load_session/save_turn`、`BaseMemory` 新增会话历史便捷方法、`memory/__init__.py` 与 `stores/__init__.py` 对 Redis 后端改为 PEP 562 惰性导入以保证核心引擎可正常导入）；2.2 已完成（`SessionKey` 三元组 + 身份绑定的 `load_session/save_turn`、API 强制 `session_id` 去掉 `default-session`、越权 404、匿名命名空间隔离、TTL 生效）；2.3 已实现（`memory/vector/` 向量检索基建 + `LongTermSemanticMemory` 重写为 embedding + ANN 真实现，见上文落地情况），⏳ **验收测试未执行**；2.7 已完成（WORKING/TOOL_RESULT/LONG_TERM 三类记忆自动沉淀管道接入引擎，见上文落地情况）；2.4 已完成（`memory/factory.py` 装配工厂 + `get_agent()` 接入 + 7 项验收测试，见上文落地情况）；2.5 已完成（`trim_messages_to_token_budget` + `HISTORY_TOKEN_BUDGET` 配置 + `load_session`/`WindowBufferMemory` 预算裁剪 + 截断事件指标 + 工厂透传 + 15 项验收测试，见上文落地情况）；2.6 已完成（`enforce_tenant_isolation` 开关 + `write/read`/`clear_session`/`clear_user` 四入口硬闸 + SessionKey 路径天然过闸 + `ENFORCE_TENANT_ISOLATION` 配置 + 工厂透传 + 16 项验收测试，见上文落地情况）；2.8 已完成（`memory.load/save/read/write/delete/clear_*` 全入口 span + `memory.*` 指标 + 命中率/淘汰/后端健康度 + `/metrics` 输出 + 12 项验收测试，见上文落地情况）。**8 项差距中 7 项完成，仅剩 2.3 验收测试待执行。**
> 建议实施顺序：2.3 待补验收测试（写召闭环/跨用户隔离/`vector_index_name` 生效，PgVectorStore 需真实 PG + pgvector 环境）；`tools/builtin/retrieval.py` 与 Prompts 2.8 动态少样本可复用已建成的 `memory/vector/` 基建统一建设。

---

## 4. 相关代码与文档索引

- 记忆模块：[memory/base.py](file:///d:/code/react-ai/src/react_ai/memory/base.py) · [memory/in_memory.py](file:///d:/code/react-ai/src/react_ai/memory/in_memory.py) · [memory/redis.py](file:///d:/code/react-ai/src/react_ai/memory/redis.py) · [memory/short_term.py](file:///d:/code/react-ai/src/react_ai/memory/short_term.py) · [memory/long_term.py](file:///d:/code/react-ai/src/react_ai/memory/long_term.py) · [memory/stores/](file:///d:/code/react-ai/src/react_ai/memory/stores/) · 装配工厂 [memory/factory.py](file:///d:/code/react-ai/src/react_ai/memory/factory.py)（2.4 新增：`create_memory_from_settings`） · 向量检索基建 [memory/vector/](file:///d:/code/react-ai/src/react_ai/memory/vector/)（2.3 新增：embedding 提供方 + pgvector/内存向量库）
- 核心引擎（待接入点）：[core/engine.py](file:///d:/code/react-ai/src/react_ai/core/engine.py) `ReActAgent.stream/_loop` · [core/context.py](file:///d:/code/react-ai/src/react_ai/core/context.py) `RunContext` · [core/lifecycle.py](file:///d:/code/react-ai/src/react_ai/core/lifecycle.py)
- API 接入点：[api/routes/agent.py](file:///d:/code/react-ai/api/routes/agent.py) `ChatRequest.session_id`
- 配置：[config/settings.py#L24-L43](file:///d:/code/react-ai/src/react_ai/config/settings.py#L24-L43) `MemoryConfig`
- 可复用基建：[utils/token_counter.py](file:///d:/code/react-ai/src/react_ai/utils/token_counter.py) · [tools/builtin/retrieval.py](file:///d:/code/react-ai/src/react_ai/tools/builtin/retrieval.py) · 审计工厂示范 [observability/audit.py](file:///d:/code/react-ai/src/react_ai/observability/audit.py)
- 测试：[tests/unit/test_memory.py](file:///d:/code/react-ai/tests/unit/test_memory.py)（依赖 fakeredis，本机不可跑，见 AGENTS.md 3.1）
- 参照文档：[Prompts-Gap-Analysis.md](file:///d:/code/react-ai/wiki/Prompts-Gap-Analysis.md)（2.1 孤岛、2.10 装配缺口同构） · [Memory.md](file:///d:/code/react-ai/wiki/Memory.md)
