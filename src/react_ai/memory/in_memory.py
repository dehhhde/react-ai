"""基于内存的记忆存储实现 (适用于开发、单机部署与单元测试)."""

import logging

from react_ai.memory.base import (
    BaseMemory,
    MemoryItem,
    MemoryQuery,
)

logger = logging.getLogger(__name__)


class InMemoryMemory(BaseMemory):
    """基于内存字典与线程锁的高性能记忆存储类。

    特性:
    - 内存存储: 数据保存在进程内存字典中，读写极快，无需外部服务；
    - 线程安全: 继承 BaseMemory 的线程重入锁保障并发操作安全；
    - 多维度索引与过滤: 支持 key、type、session_id、user_id、tenant_id 精确过滤；
    - 智能淘汰策略: 支持 LRU (最近最少更新)、LFU (最少访问次数) 或 FIFO (最早创建)，超容量时优先主动清理过期数据。
    """

    name: str = "in_memory"

    def __init__(
        self,
        name: str = "in_memory",
        max_items: int = 10000,
        eviction_policy: str = "lru",
    ):
        """初始化内存记忆存储。

        Args:
            name: 记忆实例标识名称。
            max_items: 最大允许的条目数，超过触发淘汰清理。
            eviction_policy: 超出容量时的淘汰策略 ("lru", "lfu", "fifo")。
        """
        super().__init__(name=name, max_items=max_items)
        self.eviction_policy = eviction_policy.lower()
        self._storage: dict[str, MemoryItem] = {}

    def _do_write(self, item: MemoryItem) -> MemoryItem:
        """底层写入逻辑：存入内部字典。

        若为更新已有记录，通过抵消 BaseMemory.write 的递增操作保持条目计数精确。
        """
        is_update = item.id in self._storage
        if is_update:
            self._dec_count()
        self._storage[item.id] = item
        return item

    def _do_read(self, query: MemoryQuery) -> list[MemoryItem]:
        """底层读取逻辑：遍历匹配多维度查询过滤条件。"""
        matched: list[MemoryItem] = []

        for item in self._storage.values():
            # 1. 过期校验
            if not query.include_expired and item.is_expired():
                continue

            # 2. Key 精确匹配
            if query.key is not None and item.key != query.key:
                continue

            # 3. MemoryType 过滤
            if query.type is not None and item.type != query.type:
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

            # 4. 会话隔离过滤
            if query.session_id is not None and item.session_id != query.session_id:
                continue

            # 5. 用户隔离过滤
            if query.user_id is not None and item.user_id != query.user_id:
                continue

            # 6. 租户隔离过滤
            if query.tenant_id is not None and item.tenant_id != query.tenant_id:
                continue

            matched.append(item)

        # 保持时间顺序（默认创建时间由先到后）
        matched.sort(key=lambda x: x.created_at)

        # 限制数量截断
        if query.limit and query.limit > 0:
            matched = matched[: query.limit]

        return matched

    def _do_delete(self, memory_id: str) -> bool:
        """底层删除逻辑：从内部字典移除单条记忆。"""
        if memory_id in self._storage:
            del self._storage[memory_id]
            return True
        return False

    def _evict(self):
        """淘汰策略实现：

        第一阶段：扫描并主动清理所有已过期的记忆；
        第二阶段：若仍超出上限，依据 eviction_policy 释放空间至 90% 容量。
        """
        # 阶段一：清理所有已过期项
        expired_ids = [mid for mid, it in self._storage.items() if it.is_expired()]
        for mid in expired_ids:
            del self._storage[mid]
            self._dec_count()

        if self._item_count <= self.max_items:
            logger.info(
                "in_memory_eviction_expired_done name=%s freed=%d",
                self.name,
                len(expired_ids),
            )
            return

        # 阶段二：按照指定策略淘汰正常记忆至安全阈值 (90% 容量)
        target_capacity = int(self.max_items * 0.9)
        needed_to_remove = self._item_count - target_capacity

        items_list = list(self._storage.values())
        if self.eviction_policy == "lfu":
            # 访问次数最少优先，其次最近更新时间最早
            items_list.sort(key=lambda x: (x.access_count, x.updated_at))
        elif self.eviction_policy == "fifo":
            # 最早创建优先
            items_list.sort(key=lambda x: x.created_at)
        else:
            # 默认 LRU: 最近最少更新时间优先
            items_list.sort(key=lambda x: x.updated_at)

        to_remove = items_list[:needed_to_remove]
        for it in to_remove:
            if it.id in self._storage:
                del self._storage[it.id]
                self._dec_count()

        logger.warning(
            "in_memory_eviction_policy_done name=%s policy=%s evicted=%d new_count=%d",
            self.name,
            self.eviction_policy,
            len(to_remove),
            self._item_count,
        )

    def get(self, memory_id: str) -> MemoryItem | None:
        """直接按 ID 获取单条记忆（若过期且不包含过期项则返回 None）。"""
        with self._lock:
            item = self._storage.get(memory_id)
            if item and not item.is_expired():
                item.access_count += 1
                return item
            return None

    def clear(self) -> int:
        """清空当前存储的所有记忆，返回清理的记录总数。"""
        with self._lock:
            total = len(self._storage)
            self._storage.clear()
            self._item_count = 0
            logger.info("in_memory_clear_all name=%s count=%d", self.name, total)
            return total

    def __len__(self) -> int:
        """返回存储条目总数。"""
        return len(self._storage)

    def __contains__(self, memory_id: str) -> bool:
        """判断记忆 ID 是否存在。"""
        return memory_id in self._storage
