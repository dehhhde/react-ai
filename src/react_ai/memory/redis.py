"""基于 Redis 的分布式记忆存储实现 (支持多实例共享、TTL 自动过期与多维索引检索)."""

import json
import logging
from typing import Any

import redis

from react_ai.memory.base import (
    BaseMemory,
    MemoryItem,
    MemoryQuery,
)

logger = logging.getLogger(__name__)


class RedisMemory(BaseMemory):
    """基于 Redis 实现的分布式高并发记忆存储类。

    设计特点:
    - 多维度索引: 基于 Redis ZSet 建立 session_id、user_id、tenant_id、type、key 倒排索引；
    - 原生 TTL 支持: 针对单条记忆设置 Redis PX 毫秒级过期，结合客户端逻辑双重兜底；
    - 批量检索优化: 使用 Redis Pipeline 与 MGET 批量读写，最小化网络往返延迟 (RTT)；
    - 惰性连接与灵活配置: 支持稍后注入 redis_url 或主机/端口/密码等连接参数；
    - 自动清理与驱逐: 读操作自动剔除失效索引，超容量时基于 ZSet 权重自动执行 LRU 驱逐。
    """

    name: str = "redis_memory"

    def __init__(
        self,
        redis_url: str | None = None,
        host: str = "localhost",
        port: int = 6379,
        db: int = 0,
        password: str | None = None,
        ssl: bool = False,
        client: redis.Redis | None = None,
        key_prefix: str = "react_ai:memory:",
        name: str = "redis_memory",
        max_items: int = 10000,
        socket_timeout: float = 5.0,
        lazy_connect: bool = True,
    ):
        """初始化 Redis 记忆存储实例。

        Args:
            redis_url: Redis 连接字符串，例如 "redis://:password@127.0.0.1:6379/0"。
            host: Redis 主机地址 (默认 "localhost")。
            port: Redis 端口 (默认 6379)。
            db: Redis 数据库序号 (默认 0)。
            password: Redis 访问密码。
            ssl: 是否启用 SSL 连接。
            client: 外部已初始化的 redis.Redis 实例 (若提供则优先使用)。
            key_prefix: 存储在 Redis 中的统一 Key 命名空间前缀。
            name: 记忆模块实例标识。
            max_items: 最大允许记忆条目数，超过触发 LRU 淘汰。
            socket_timeout: 网络操作超时时长（秒）。
            lazy_connect: 是否惰性连接（True 表示在首次数据操作或 ping 时才建立连接）。
        """
        super().__init__(name=name, max_items=max_items)
        self.key_prefix = key_prefix
        self.socket_timeout = socket_timeout
        # 若未提供显式连接参数且无外部 client，则自动从全局 settings 继承配置
        if (
            redis_url is None
            and client is None
            and host == "localhost"
            and port == 6379
            and password is None
        ):
            try:
                from react_ai.config.settings import settings

                if (
                    settings.memory.redis_url
                    or settings.memory.password
                    or settings.memory.port != 6379
                ):
                    redis_url = settings.memory.resolved_redis_url
                    host = settings.memory.host
                    port = settings.memory.port
                    password = settings.memory.password
                    db = settings.memory.db
            except (ImportError, AttributeError) as exc:
                logger.debug("redis_memory_settings_load_skip error=%s", exc)

        self._redis_url = redis_url
        self._host = host
        self._port = port
        self._db = db
        self._password = password
        self._ssl = ssl
        self._client: redis.Redis | None = client

        if not lazy_connect and client is None:
            self._ensure_client()

    def configure(
        self,
        redis_url: str | None = None,
        host: str | None = None,
        port: int | None = None,
        db: int | None = None,
        password: str | None = None,
        ssl: bool | None = None,
        client: redis.Redis | None = None,
    ):
        """动态配置或更新 Redis 连接参数。用户在后续提供配置时可随时调用此方法生效。"""
        with self._lock:
            if client is not None:
                self._client = client
                self._redis_url = None
            else:
                if redis_url is not None:
                    self._redis_url = redis_url
                if host is not None:
                    self._host = host
                if port is not None:
                    self._port = port
                if db is not None:
                    self._db = db
                if password is not None:
                    self._password = password
                if ssl is not None:
                    self._ssl = ssl
                self._client = None  # 重置旧连接

            # 重新同步容量计数
            try:
                self._sync_item_count()
            except (redis.RedisError, ConnectionError) as e:
                logger.warning("redis_configure_sync_count_deferred error=%s", e)

    @classmethod
    def from_url(
        cls,
        redis_url: str,
        key_prefix: str = "react_ai:memory:",
        name: str = "redis_memory",
        max_items: int = 10000,
        **kwargs,
    ) -> "RedisMemory":
        """便捷工厂方法：直接通过 Redis URL 实例化。"""
        return cls(
            redis_url=redis_url,
            key_prefix=key_prefix,
            name=name,
            max_items=max_items,
            **kwargs,
        )

    @classmethod
    def from_client(
        cls,
        client: redis.Redis,
        key_prefix: str = "react_ai:memory:",
        name: str = "redis_memory",
        max_items: int = 10000,
    ) -> "RedisMemory":
        """便捷工厂方法：使用现有 Redis 客户端对象实例化。"""
        return cls(
            client=client,
            key_prefix=key_prefix,
            name=name,
            max_items=max_items,
        )

    @property
    def client(self) -> redis.Redis:
        """获取当前活跃的 Redis 客户端连接。"""
        return self._ensure_client()

    def _ensure_client(self) -> redis.Redis:
        if self._client is not None:
            return self._client

        with self._lock:
            if self._client is not None:
                return self._client

            try:
                if self._redis_url:
                    self._client = redis.Redis.from_url(
                        self._redis_url,
                        decode_responses=True,
                        socket_timeout=self.socket_timeout,
                    )
                else:
                    self._client = redis.Redis(
                        host=self._host,
                        port=self._port,
                        db=self._db,
                        password=self._password,
                        ssl=self._ssl,
                        decode_responses=True,
                        socket_timeout=self.socket_timeout,
                    )
                self._sync_item_count()
            except Exception as exc:
                raise ConnectionError(
                    f"无法连接到 Redis 服务 ({exc})。请使用 RedisMemory(redis_url='...') "
                    "或 memory.configure(...) 提供有效的 Redis 连接配置。"
                ) from exc

            return self._client

    def is_connected(self) -> bool:
        """检查 Redis 服务是否连通可用。"""
        try:
            return bool(self.client.ping())
        except (redis.RedisError, ConnectionError):
            return False

    def _sync_item_count(self):
        """从 Redis 同步当前记忆条目总数。"""
        try:
            if self._client:
                count = self._client.zcard(self._idx_all_key())
                self._item_count = count or 0
        except (redis.RedisError, ConnectionError):
            pass

    # ==================== Redis Key 命名规范 ====================
    def _item_key(self, memory_id: str) -> str:
        return f"{self.key_prefix}item:{memory_id}"

    def _idx_all_key(self) -> str:
        return f"{self.key_prefix}idx:all"

    def _idx_session_key(self, session_id: str) -> str:
        return f"{self.key_prefix}idx:session:{session_id}"

    def _idx_user_key(self, user_id: str) -> str:
        return f"{self.key_prefix}idx:user:{user_id}"

    def _idx_tenant_key(self, tenant_id: str) -> str:
        return f"{self.key_prefix}idx:tenant:{tenant_id}"

    def _idx_type_key(self, mem_type: str) -> str:
        return f"{self.key_prefix}idx:type:{mem_type}"

    def _idx_key_name_key(self, key_name: str) -> str:
        return f"{self.key_prefix}idx:key:{key_name}"

    # ==================== 核心抽象方法实现 ====================
    def _do_write(self, item: MemoryItem) -> MemoryItem:
        """底层写入逻辑：序列化 JSON 写入 Redis 并维护关联索引。"""
        r = self.client
        item_key = self._item_key(item.id)
        type_str = item.type.value if hasattr(item.type, "value") else str(item.type)

        # 检查是否为覆盖已有记录
        is_existing = bool(r.exists(item_key))
        if is_existing:
            self._dec_count()  # 抵消 BaseMemory.write 内部的 _inc_count
            # 读取旧数据以清理旧索引
            old_raw = r.get(item_key)
            if old_raw:
                try:
                    old_item = MemoryItem.from_dict(json.loads(old_raw))
                    self._remove_indexes_from_item(r, old_item)
                except (json.JSONDecodeError, KeyError, ValueError):
                    pass

        payload = json.dumps(item.to_dict(), ensure_ascii=False)
        pipe = r.pipeline(transaction=True)

        # 1. 写入实体数据，若指定了 TTL 则设置毫秒级过期
        if item.ttl_seconds is not None and item.ttl_seconds > 0:
            px_ms = int(item.ttl_seconds * 1000)
            pipe.set(item_key, payload, px=px_ms)
        else:
            pipe.set(item_key, payload)

        # 2. 维护各维度 ZSet 索引 (以 updated_at 作为分数，便于 LRU 淘汰和时间范围排序)
        score = item.updated_at
        pipe.zadd(self._idx_all_key(), {item.id: score})

        if item.session_id:
            pipe.zadd(self._idx_session_key(item.session_id), {item.id: score})
        if item.user_id:
            pipe.zadd(self._idx_user_key(item.user_id), {item.id: score})
        if item.tenant_id:
            pipe.zadd(self._idx_tenant_key(item.tenant_id), {item.id: score})
        if type_str:
            pipe.zadd(self._idx_type_key(type_str), {item.id: score})
        if item.key:
            pipe.zadd(self._idx_key_name_key(item.key), {item.id: score})

        pipe.execute()
        return item

    def _do_read(self, query: MemoryQuery) -> list[MemoryItem]:
        """底层读取逻辑：基于索引集合筛选候选 ID，并通过 MGET 批量拉取反序列化。"""
        r = self.client
        candidate_ids = self._get_candidate_ids(query)
        if not candidate_ids:
            return []

        # 批量获取 Redis 实体数据
        keys = [self._item_key(mid) for mid in candidate_ids]
        raw_items = r.mget(keys)

        items: list[MemoryItem] = []
        stale_ids: list[str] = []

        for mid, raw in zip(candidate_ids, raw_items):
            if raw is None:
                # Key 已在 Redis 中被原生 TTL 删除或主动删除，记录以便清理残留索引
                stale_ids.append(mid)
                continue

            try:
                data = json.loads(raw)
                item = MemoryItem.from_dict(data)
            except (json.JSONDecodeError, KeyError, ValueError) as e:
                logger.warning(
                    "redis_memory_deserialization_error id=%s err=%s", mid, e
                )
                continue

            # 内存级 TTL 再次校验兜底
            if not query.include_expired and item.is_expired():
                stale_ids.append(mid)
                continue

            # 精确字段比对过滤
            if query.key is not None and item.key != query.key:
                continue
            if query.type is not None:
                item_type_val = (
                    item.type.value if hasattr(item.type, "value") else str(item.type)
                )
                query_type_val = (
                    query.type.value
                    if hasattr(query.type, "value")
                    else str(query.type)
                )
                if item_type_val != query_type_val:
                    continue
            if query.session_id is not None and item.session_id != query.session_id:
                continue
            if query.user_id is not None and item.user_id != query.user_id:
                continue
            if query.tenant_id is not None and item.tenant_id != query.tenant_id:
                continue

            items.append(item)

        # 异步清理过期产生的僵尸索引
        if stale_ids:
            self._cleanup_stale_ids(stale_ids)

        # 默认按创建时间排序
        items.sort(key=lambda x: x.created_at)

        # 数量截断
        if query.limit and query.limit > 0:
            items = items[: query.limit]

        return items

    def _do_delete(self, memory_id: str) -> bool:
        """底层删除逻辑：删除实体数据及关联维度索引。"""
        r = self.client
        item_key = self._item_key(memory_id)
        raw = r.get(item_key)

        pipe = r.pipeline(transaction=True)
        pipe.delete(item_key)
        pipe.zrem(self._idx_all_key(), memory_id)

        if raw:
            try:
                data = json.loads(raw)
                item = MemoryItem.from_dict(data)
                self._remove_indexes_from_item(pipe, item)
            except (json.JSONDecodeError, KeyError, ValueError):
                pass

        results = pipe.execute()
        # 只要实体 key 被删除或索引被移除则视作成功
        return bool(results[0] > 0 or results[1] > 0)

    def _evict(self):
        """淘汰策略实现：超容量时拉取最久未更新条目 (LRU) 进行主动驱逐。"""
        r = self.client
        target_capacity = int(self.max_items * 0.9)
        current_count = r.zcard(self._idx_all_key()) or 0

        if current_count <= target_capacity:
            self._item_count = current_count
            return

        needed = current_count - target_capacity
        # ZSet 分数为 updated_at，排名靠前者为最少更新的项
        oldest_ids = r.zrange(self._idx_all_key(), 0, needed - 1)
        if not oldest_ids:
            return

        for mid in oldest_ids:
            self._do_delete(mid)
            self._dec_count()

        logger.warning(
            "redis_memory_evicted count=%d target=%d new_count=%d",
            len(oldest_ids),
            target_capacity,
            self._item_count,
        )

    # ==================== 辅助索引检索方法 ====================
    def _get_candidate_ids(self, query: MemoryQuery) -> list[str]:
        """根据查询条件选取最相关的 ZSet 索引获取候选 ID 列表。"""
        r = self.client
        index_keys: list[str] = []

        if query.session_id:
            index_keys.append(self._idx_session_key(query.session_id))
        if query.user_id:
            index_keys.append(self._idx_user_key(query.user_id))
        if query.tenant_id:
            index_keys.append(self._idx_tenant_key(query.tenant_id))
        if query.key:
            index_keys.append(self._idx_key_name_key(query.key))
        if query.type:
            type_val = (
                query.type.value if hasattr(query.type, "value") else str(query.type)
            )
            index_keys.append(self._idx_type_key(type_val))

        if not index_keys:
            # 无任何限定索引时扫描全局集合
            all_ids = r.zrange(self._idx_all_key(), 0, -1)
            return all_ids or []

        # 单个索引直接获取
        if len(index_keys) == 1:
            ids = r.zrange(index_keys[0], 0, -1)
            return ids or []

        # 多个索引求交集 (Python 内存轻量求交，避免写入临时 Key)
        sets: list[set[str]] = []
        for ik in index_keys:
            members = r.zrange(ik, 0, -1)
            sets.append(set(members or []))

        if not sets:
            return []
        intersected = set.intersection(*sets)
        return list(intersected)

    def _remove_indexes_from_item(self, pipe_or_client, item: MemoryItem):
        """从各维度索引中剥离单条记忆的引用。"""
        mid = item.id
        pipe_or_client.zrem(self._idx_all_key(), mid)
        if item.session_id:
            pipe_or_client.zrem(self._idx_session_key(item.session_id), mid)
        if item.user_id:
            pipe_or_client.zrem(self._idx_user_key(item.user_id), mid)
        if item.tenant_id:
            pipe_or_client.zrem(self._idx_tenant_key(item.tenant_id), mid)
        if item.key:
            pipe_or_client.zrem(self._idx_key_name_key(item.key), mid)
        type_str = item.type.value if hasattr(item.type, "value") else str(item.type)
        if type_str:
            pipe_or_client.zrem(self._idx_type_key(type_str), mid)

    def _cleanup_stale_ids(self, stale_ids: list[str]):
        """批量清理失效或过期的索引引用。"""
        try:
            r = self.client
            pipe = r.pipeline(transaction=False)
            for mid in stale_ids:
                pipe.delete(self._item_key(mid))
                pipe.zrem(self._idx_all_key(), mid)
            pipe.execute()
        except redis.RedisError as e:
            logger.debug("redis_cleanup_stale_error: %s", e)

    def get(self, memory_id: str) -> MemoryItem | None:
        """根据 ID 直读单条记忆。"""
        with self._lock:
            raw = self.client.get(self._item_key(memory_id))
            if not raw:
                return None
            try:
                item = MemoryItem.from_dict(json.loads(raw))
                if item.is_expired():
                    self._do_delete(memory_id)
                    return None
                item.access_count += 1
                # 写回自增的访问计数
                self.client.set(
                    self._item_key(memory_id),
                    json.dumps(item.to_dict(), ensure_ascii=False),
                )
                return item
            except (json.JSONDecodeError, KeyError, ValueError):
                return None

    def clear(self) -> int:
        """清空当前前缀下的所有记忆数据与索引。"""
        with self._lock:
            r = self.client
            keys = r.keys(f"{self.key_prefix}*")
            if keys:
                count = r.delete(*keys)
                self._item_count = 0
                return count
            return 0

    def get_stats(self) -> dict[str, Any]:
        """获取记忆存储统计信息。"""
        with self._lock:
            stats = super().get_stats()
            stats.update(
                {
                    "key_prefix": self.key_prefix,
                    "redis_url_configured": bool(self._redis_url or self._host),
                }
            )
            return stats
