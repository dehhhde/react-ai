"""进程内存向量存储：余弦相似度暴力检索，用于开发/测试与单机降级场景。"""

import logging
import threading
import time
from collections import defaultdict

from react_ai.memory.vector.base import (
    BaseVectorStore,
    SearchHit,
    VectorRecord,
    cosine_similarity,
)

logger = logging.getLogger(__name__)


class InMemoryVectorStore(BaseVectorStore):
    """线程安全的内存向量库。

    - 按索引名分桶存储，检索在桶内做全量余弦相似度排序（暴力 ANN）；
    - user_id/tenant_id 过滤在打分前完成，保证跨身份不可见；
    - 进程重启即丢失，生产多副本场景应使用 PgVectorStore。
    """

    def __init__(self, max_records_per_index: int = 100000):
        self._lock = threading.RLock()
        self._indexes: dict[str, dict[str, VectorRecord]] = defaultdict(dict)
        self.max_records_per_index = max_records_per_index

    async def upsert(self, index: str, records: list[VectorRecord]) -> None:
        with self._lock:
            bucket = self._indexes[index]
            for record in records:
                if record.created_at <= 0:
                    record.created_at = time.time()
                bucket[record.id] = record
            if len(bucket) > self.max_records_per_index:
                logger.warning(
                    "vector_index_capacity_exceeded index=%s count=%d max=%d",
                    index,
                    len(bucket),
                    self.max_records_per_index,
                )

    async def search(
        self,
        index: str,
        query_vector: list[float],
        top_k: int = 5,
        *,
        user_id: str | None = None,
        tenant_id: str | None = None,
    ) -> list[SearchHit]:
        if top_k <= 0:
            return []
        with self._lock:
            candidates = list(self._indexes.get(index, {}).values())

        # 身份过滤先于打分：不匹配的记录完全不参与召回
        if user_id is not None:
            candidates = [r for r in candidates if r.user_id == user_id]
        if tenant_id is not None:
            candidates = [r for r in candidates if r.tenant_id == tenant_id]

        hits = [
            SearchHit(record=r, score=cosine_similarity(query_vector, r.vector))
            for r in candidates
        ]
        hits.sort(key=lambda h: h.score, reverse=True)
        return hits[:top_k]

    async def delete_by_user(
        self, index: str, user_id: str, *, tenant_id: str | None = None
    ) -> int:
        with self._lock:
            bucket = self._indexes.get(index)
            if not bucket:
                return 0
            stale_ids = [
                rid
                for rid, r in bucket.items()
                if r.user_id == user_id
                and (tenant_id is None or r.tenant_id == tenant_id)
            ]
            for rid in stale_ids:
                del bucket[rid]
            return len(stale_ids)

    def count(self, index: str) -> int:
        """当前索引的记录数，供统计与调试。"""
        with self._lock:
            return len(self._indexes.get(index, {}))
