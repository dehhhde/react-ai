"""长期语义记忆（跨会话知识召回与用户画像关联，差距分析 2.3 真实现）。

- 写入侧：事实文本 → embedding → 向量库 upsert（按 user/tenant 打身份标签）；
- 召回侧：query → embedding → ANN top-k，user_id/tenant_id 在存储层强制过滤，
  无关用户不可召回；
- ``vector_index_name`` 真实生效：作为向量索引名贯穿存储层
  （PgVectorStore 中即表名，自动建表建扩展）；
- 缺省装配零依赖可用：HashingEmbeddingProvider（确定性本地伪向量） +
  InMemoryVectorStore，生产注入 OpenAIEmbeddingProvider + PgVectorStore。
"""

import logging
import time
import uuid
from typing import Any

from react_ai.memory.vector.base import (
    BaseEmbeddingProvider,
    BaseVectorStore,
    SearchHit,
    VectorRecord,
)
from react_ai.memory.vector.embedding import HashingEmbeddingProvider
from react_ai.memory.vector.in_memory import InMemoryVectorStore
from react_ai.observability import metrics, tracer

logger = logging.getLogger(__name__)


class LongTermSemanticMemory:
    """基于向量检索的长期语义记忆。

    Args:
        embedding: 文本向量化提供方，缺省为本地哈希伪向量（离线开发用）。
        store: 向量存储，缺省为进程内存实现；生产建议注入 PgVectorStore。
        vector_index_name: 向量索引名（pgvector 表名），真实传递给存储层。
        tenant_id: 实例级默认租户；单次调用可覆盖，两者都缺省时不做租户过滤。
    """

    def __init__(
        self,
        *,
        embedding: BaseEmbeddingProvider | None = None,
        store: BaseVectorStore | None = None,
        vector_index_name: str = "agent_memories",
        tenant_id: str | None = None,
    ):
        if not vector_index_name or not vector_index_name.strip():
            raise ValueError("vector_index_name 不能为空")
        self.embedding = embedding or HashingEmbeddingProvider()
        self.store = store or InMemoryVectorStore()
        self.vector_index_name = vector_index_name.strip()
        self.default_tenant_id = tenant_id

    def _resolve_tenant(self, tenant_id: str | None) -> str | None:
        return tenant_id or self.default_tenant_id

    async def add_fact(
        self,
        user_id: str,
        fact: str,
        *,
        tenant_id: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> str:
        """沉淀一条用户事实到向量库，返回记录 ID。

        会话结束的事实抽取管道（差距分析 2.7）落地后由钩子自动调用，
        当前提供显式写入入口，替代原占位实现。
        """
        if not user_id or not str(user_id).strip():
            raise ValueError("user_id 不能为空：长期记忆必须按用户隔离")
        if not fact or not fact.strip():
            raise ValueError("fact 不能为空")

        text = fact.strip()
        start = time.perf_counter()
        ok = False
        try:
            # 链路追踪（差距分析 2.8）：memory.save span（长期记忆写入）
            async with tracer.start_span(
                "memory.save",
                {
                    "backend": self.vector_index_name,
                    "tenant_id": self._resolve_tenant(tenant_id),
                    "user_id": str(user_id).strip(),
                    "kind": "long_term_fact",
                },
            ):
                vector = (await self.embedding.embed([text]))[0]
                record = VectorRecord(
                    id=uuid.uuid4().hex,
                    text=text,
                    vector=vector,
                    user_id=str(user_id).strip(),
                    tenant_id=self._resolve_tenant(tenant_id),
                    metadata=dict(metadata or {}),
                    created_at=time.time(),
                )
                await self.store.upsert(self.vector_index_name, [record])
                logger.info(
                    "long_term_fact_added index=%s id=%s user=%s tenant=%s",
                    self.vector_index_name,
                    record.id,
                    record.user_id,
                    record.tenant_id,
                )
                ok = True
                return record.id
        finally:
            self._record_search_op(
                self._resolve_tenant(tenant_id),
                (time.perf_counter() - start) * 1000,
                result="ok" if ok else "error",
                items=1 if ok else 0,
                op="save",
            )

    async def search_relevant_facts(
        self,
        user_id: str,
        query: str,
        top_k: int = 5,
        *,
        tenant_id: str | None = None,
        min_score: float | None = None,
    ) -> list[str]:
        """按语义相关性召回该用户的历史事实（文本列表，相关度降序）。

        min_score 为相似度下限（如 0.3），用于过滤弱相关噪声；None 表示不过滤。
        """
        if not user_id or not str(user_id).strip():
            raise ValueError("user_id 不能为空：禁止跨用户全量召回")
        if top_k <= 0:
            return []

        hits = await self._search(str(user_id).strip(), query, top_k, tenant_id)
        if min_score is not None:
            hits = [hit for hit in hits if hit.score >= min_score]
        logger.info(
            "long_term_search index=%s user=%s hits=%d",
            self.vector_index_name,
            user_id,
            len(hits),
        )
        return [hit.record.text for hit in hits]

    async def search_relevant(
        self,
        user_id: str,
        query: str,
        top_k: int = 5,
        *,
        tenant_id: str | None = None,
    ) -> list[SearchHit]:
        """同 search_relevant_facts，但返回含得分与元数据的完整命中记录。"""
        if not user_id or not str(user_id).strip():
            raise ValueError("user_id 不能为空：禁止跨用户全量召回")
        return await self._search(str(user_id).strip(), query, top_k, tenant_id)

    async def _search(
        self,
        user_id: str,
        query: str,
        top_k: int,
        tenant_id: str | None,
    ) -> list[SearchHit]:
        if top_k <= 0 or not query or not query.strip():
            return []
        resolved_tenant = self._resolve_tenant(tenant_id)
        start = time.perf_counter()
        ok = False
        hits: list[SearchHit] = []
        try:
            # 链路追踪（差距分析 2.8）：memory.search span
            async with tracer.start_span(
                "memory.search",
                {
                    "backend": self.vector_index_name,
                    "tenant_id": resolved_tenant,
                    "user_id": user_id,
                    "top_k": top_k,
                },
            ) as span:
                query_vector = (await self.embedding.embed([query.strip()]))[0]
                hits = await self.store.search(
                    self.vector_index_name,
                    query_vector,
                    top_k=top_k,
                    user_id=user_id,
                    tenant_id=resolved_tenant,
                )
                span.set_attribute("hit_count", len(hits))
                ok = True
                return hits
        finally:
            # 长期记忆召回命中率（差距分析 2.8）
            self._record_search_op(
                resolved_tenant,
                (time.perf_counter() - start) * 1000,
                result=("hit" if hits else "miss") if ok else "error",
                items=len(hits),
            )

    def _record_search_op(
        self,
        tenant_id: str,
        duration_ms: float,
        *,
        result: str = "ok",
        items: int = 0,
        op: str = "search",
    ) -> None:
        """上报长期记忆读写指标；指标层异常不得阻断主流程."""
        try:
            metrics.record_memory_op(
                op=op,
                backend=self.vector_index_name,
                tenant_id=tenant_id or "",
                result=result,
                duration_ms=duration_ms,
                items=items,
            )
        except Exception:  # pragma: no cover
            logger.debug("memory_long_term_metric_emit_failed op=%s", op)

    async def forget_user(
        self, user_id: str, *, tenant_id: str | None = None
    ) -> int:
        """删除某用户的全部长期记忆（被遗忘权/账号注销），返回删除条数。"""
        if not user_id or not str(user_id).strip():
            raise ValueError("user_id 不能为空")
        resolved_tenant = self._resolve_tenant(tenant_id)
        start = time.perf_counter()
        ok = False
        deleted = 0
        try:
            # 链路追踪（差距分析 2.8）：memory.delete span（长期记忆清除）
            async with tracer.start_span(
                "memory.delete",
                {
                    "backend": self.vector_index_name,
                    "tenant_id": resolved_tenant,
                    "user_id": str(user_id).strip(),
                    "kind": "long_term_forget_user",
                },
            ) as span:
                deleted = await self.store.delete_by_user(
                    self.vector_index_name,
                    str(user_id).strip(),
                    tenant_id=resolved_tenant,
                )
                logger.info(
                    "long_term_forget_user index=%s user=%s deleted=%d",
                    self.vector_index_name,
                    user_id,
                    deleted,
                )
                span.set_attribute("deleted_count", deleted)
                ok = True
                return deleted
        finally:
            self._record_search_op(
                resolved_tenant,
                (time.perf_counter() - start) * 1000,
                result="ok" if ok else "error",
                items=deleted,
                op="delete",
            )
