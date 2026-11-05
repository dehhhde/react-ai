"""pgvector 向量存储实现（差距分析 2.3 生产方案：复用现有 PostgreSQL）。

- 索引名即表名（vector_index_name 真实生效），首次使用时自动建扩展与表；
- 余弦距离 <=> 检索，score = 1 - distance，user_id/tenant_id 在 SQL 层强制过滤；
- psycopg 为同步驱动，统一经 asyncio.to_thread 卸载，避免阻塞事件循环；
- 连接风格与 observability/audit.py 的 PostgresAuditStore 一致（惰性连接、
  幂等 DDL、dict_row）。
"""

import asyncio
import json
import logging
import re
import time
from typing import Any

from react_ai.memory.vector.base import (
    BaseVectorStore,
    SearchHit,
    VectorRecord,
)

logger = logging.getLogger(__name__)

# 表名白名单：小写字母/数字/下划线，首字符非数字，长度 ≤ 63（PG 标识符上限）
_INDEX_NAME_RE = re.compile(r"^[a-z_][a-z0-9_]{0,62}$")


def _validate_index_name(index: str) -> str:
    """校验并规范化索引名（表名无法参数化，必须白名单防注入）。"""
    normalized = index.strip().lower()
    if not _INDEX_NAME_RE.match(normalized):
        raise ValueError(
            f"非法向量索引名 {index!r}：仅允许小写字母/数字/下划线，且以字母或下划线开头"
        )
    return normalized


def _vector_literal(vector: list[float]) -> str:
    """float 列表 → pgvector 文本字面量 '[0.1,0.2,...]'。"""
    return "[" + ",".join(repr(float(v)) for v in vector) + "]"


class PgVectorStore(BaseVectorStore):
    """基于 pgvector 扩展的持久化向量存储。

    要求目标 PostgreSQL 已安装 pgvector 扩展二进制；首次写入时执行
    CREATE EXTENSION IF NOT EXISTS vector（需相应权限）。
    """

    def __init__(self, dsn: str | None = None, connect_timeout: float = 5.0):
        if dsn is None:
            from react_ai.config.settings import settings

            dsn = settings.database.url
        self.dsn = dsn
        self.connect_timeout = connect_timeout
        self._ensured: set[str] = set()

    def _connect(self):
        import psycopg
        from psycopg.rows import dict_row

        return psycopg.connect(
            self.dsn, row_factory=dict_row, connect_timeout=self.connect_timeout
        )

    # ------------------------------------------------------------------
    # 同步实现（被 async 方法经 asyncio.to_thread 调用）
    # ------------------------------------------------------------------

    def _ensure_table_sync(self, table: str, dimensions: int) -> None:
        ddl = f"""
        CREATE EXTENSION IF NOT EXISTS vector;
        CREATE TABLE IF NOT EXISTS {table} (
            id         TEXT PRIMARY KEY,
            text       TEXT NOT NULL,
            user_id    TEXT,
            tenant_id  TEXT,
            metadata   JSONB NOT NULL DEFAULT '{{}}'::jsonb,
            created_at DOUBLE PRECISION NOT NULL,
            embedding  vector({dimensions}) NOT NULL
        );
        CREATE INDEX IF NOT EXISTS {table}_user_idx ON {table}(user_id);
        CREATE INDEX IF NOT EXISTS {table}_tenant_idx ON {table}(tenant_id);
        """
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(ddl)
            conn.commit()
        self._ensured.add(table)
        logger.info("pgvector_table_ensured table=%s dimensions=%d", table, dimensions)

    def _upsert_sync(self, table: str, records: list[VectorRecord]) -> None:
        if table not in self._ensured:
            self._ensure_table_sync(table, len(records[0].vector))
        sql = f"""
        INSERT INTO {table}
            (id, text, user_id, tenant_id, metadata, created_at, embedding)
        VALUES (%s, %s, %s, %s, %s::jsonb, %s, %s::vector)
        ON CONFLICT (id) DO UPDATE SET
            text = EXCLUDED.text,
            user_id = EXCLUDED.user_id,
            tenant_id = EXCLUDED.tenant_id,
            metadata = EXCLUDED.metadata,
            created_at = EXCLUDED.created_at,
            embedding = EXCLUDED.embedding
        """
        now = time.time()
        rows = [
            (
                r.id,
                r.text,
                r.user_id,
                r.tenant_id,
                json.dumps(r.metadata or {}, ensure_ascii=False, default=str),
                r.created_at if r.created_at > 0 else now,
                _vector_literal(r.vector),
            )
            for r in records
        ]
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.executemany(sql, rows)
            conn.commit()

    def _search_sync(
        self,
        table: str,
        query_vector: list[float],
        top_k: int,
        user_id: str | None,
        tenant_id: str | None,
    ) -> list[SearchHit]:
        # 命名参数：query 向量在 SELECT 打分与 ORDER BY 排序中复用同一值
        conditions: list[str] = []
        params: dict[str, Any] = {"q": _vector_literal(query_vector), "k": top_k}
        if user_id is not None:
            conditions.append("user_id = %(uid)s")
            params["uid"] = user_id
        if tenant_id is not None:
            conditions.append("tenant_id = %(tid)s")
            params["tid"] = tenant_id
        where = f"WHERE {' AND '.join(conditions)}" if conditions else ""

        sql = f"""
        SELECT id, text, user_id, tenant_id, metadata, created_at,
               1 - (embedding <=> %(q)s::vector) AS score
        FROM {table}
        {where}
        ORDER BY embedding <=> %(q)s::vector
        LIMIT %(k)s
        """
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(sql, params)
                rows = cur.fetchall()

        hits: list[SearchHit] = []
        for row in rows:
            metadata = row["metadata"]
            if isinstance(metadata, str):
                metadata = json.loads(metadata)
            hits.append(
                SearchHit(
                    record=VectorRecord(
                        id=row["id"],
                        text=row["text"],
                        vector=[],  # pgvector 不回传全量向量，召回方仅需文本
                        user_id=row["user_id"],
                        tenant_id=row["tenant_id"],
                        metadata=metadata or {},
                        created_at=row["created_at"],
                    ),
                    score=float(row["score"]),
                )
            )
        return hits

    def _delete_by_user_sync(
        self, table: str, user_id: str, tenant_id: str | None
    ) -> int:
        sql = f"DELETE FROM {table} WHERE user_id = %s"
        params: list[Any] = [user_id]
        if tenant_id is not None:
            sql += " AND tenant_id = %s"
            params.append(tenant_id)
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(sql, params)
                deleted = cur.rowcount
            conn.commit()
        return deleted or 0

    # ------------------------------------------------------------------
    # BaseVectorStore 异步接口
    # ------------------------------------------------------------------

    async def ensure_index(self, index: str, dimensions: int) -> None:
        table = _validate_index_name(index)
        if table in self._ensured:
            return
        await asyncio.to_thread(self._ensure_table_sync, table, dimensions)

    async def upsert(self, index: str, records: list[VectorRecord]) -> None:
        if not records:
            return
        table = _validate_index_name(index)
        await asyncio.to_thread(self._upsert_sync, table, records)
        logger.info("pgvector_upsert table=%s count=%d", table, len(records))

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
        table = _validate_index_name(index)
        return await asyncio.to_thread(
            self._search_sync, table, query_vector, top_k, user_id, tenant_id
        )

    async def delete_by_user(
        self, index: str, user_id: str, *, tenant_id: str | None = None
    ) -> int:
        table = _validate_index_name(index)
        return await asyncio.to_thread(
            self._delete_by_user_sync, table, user_id, tenant_id
        )
