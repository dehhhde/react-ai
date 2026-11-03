"""工具调用审计存储：记录每次工具调用的完整信息，支持查询回放.

遵循项目 CheckpointStore 模式：ABC + InMemory 默认实现 + PostgreSQL 实现。
PostgreSQL 实现使用 psycopg（与 aftersales 工具一致），表结构自动创建。

审计记录字段：谁（tenant/user）、何时、何工具、何参数（摘要）、结果状态、
错误类型/码、耗时、trace 关联。
"""
from __future__ import annotations

import json
import time
from abc import ABC, abstractmethod
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field


class ToolCallAuditRecord(BaseModel):
    """一次工具调用的审计记录."""

    audit_id: str = Field(default_factory=lambda: __import__("uuid").uuid4().hex)
    trace_id: str
    tool_call_id: Optional[str] = None
    tool_name: str
    tenant_id: str
    user_id: str
    arguments: Dict[str, Any] = Field(default_factory=dict)
    side_effects: str = "none"
    success: bool
    error_type: Optional[str] = None
    error_code: Optional[str] = None
    error_message: Optional[str] = None
    duration_ms: float = 0.0
    replayed: bool = False  # 幂等重放或缓存命中
    created_at: float = Field(default_factory=time.time)


class AuditStore(ABC):
    """审计存储抽象."""

    @abstractmethod
    def record(self, record: ToolCallAuditRecord) -> None: ...

    @abstractmethod
    def query(
        self,
        *,
        trace_id: Optional[str] = None,
        tool_name: Optional[str] = None,
        tenant_id: Optional[str] = None,
        limit: int = 100,
    ) -> List[ToolCallAuditRecord]: ...


class InMemoryAuditStore(AuditStore):
    """内存审计存储（开发/测试用），按时间倒序返回."""

    def __init__(self, max_records: int = 10000):
        self._records: List[ToolCallAuditRecord] = []
        self._max_records = max_records

    def record(self, record: ToolCallAuditRecord) -> None:
        self._records.append(record)
        if len(self._records) > self._max_records:
            self._records = self._records[-self._max_records :]

    def query(
        self,
        *,
        trace_id: Optional[str] = None,
        tool_name: Optional[str] = None,
        tenant_id: Optional[str] = None,
        limit: int = 100,
    ) -> List[ToolCallAuditRecord]:
        result = self._records
        if trace_id:
            result = [r for r in result if r.trace_id == trace_id]
        if tool_name:
            result = [r for r in result if r.tool_name == tool_name]
        if tenant_id:
            result = [r for r in result if r.tenant_id == tenant_id]
        # 时间倒序
        return sorted(result, key=lambda r: r.created_at, reverse=True)[:limit]


class PostgresAuditStore(AuditStore):
    """PostgreSQL 审计存储，使用 psycopg（与 aftersales 工具一致）.

    表结构自动创建，幂等安全。
    """

    CREATE_TABLE_SQL = """
    CREATE TABLE IF NOT EXISTS tool_call_audit (
        audit_id       TEXT PRIMARY KEY,
        trace_id       TEXT NOT NULL,
        tool_call_id   TEXT,
        tool_name      TEXT NOT NULL,
        tenant_id      TEXT NOT NULL,
        user_id        TEXT NOT NULL,
        arguments      JSONB NOT NULL DEFAULT '{}'::jsonb,
        side_effects   TEXT NOT NULL DEFAULT 'none',
        success        BOOLEAN NOT NULL,
        error_type     TEXT,
        error_code     TEXT,
        error_message  TEXT,
        duration_ms    DOUBLE PRECISION NOT NULL DEFAULT 0,
        replayed       BOOLEAN NOT NULL DEFAULT FALSE,
        created_at     DOUBLE PRECISION NOT NULL
    );
    CREATE INDEX IF NOT EXISTS idx_tool_call_audit_trace ON tool_call_audit(trace_id);
    CREATE INDEX IF NOT EXISTS idx_tool_call_audit_tool  ON tool_call_audit(tool_name);
    CREATE INDEX IF NOT EXISTS idx_tool_call_audit_tenant ON tool_call_audit(tenant_id);
    """

    def __init__(self, db_url: Optional[str] = None):
        from react_ai.config.settings import settings

        self.db_url = db_url or settings.database.url

    def _connect(self):
        import psycopg
        from psycopg.rows import dict_row

        return psycopg.connect(self.db_url, row_factory=dict_row)

    def ensure_table(self) -> None:
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(self.CREATE_TABLE_SQL)
            conn.commit()

    def record(self, record: ToolCallAuditRecord) -> None:
        try:
            with self._connect() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        INSERT INTO tool_call_audit
                        (audit_id, trace_id, tool_call_id, tool_name, tenant_id, user_id,
                         arguments, side_effects, success, error_type, error_code,
                         error_message, duration_ms, replayed, created_at)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                        """,
                        (
                            record.audit_id,
                            record.trace_id,
                            record.tool_call_id,
                            record.tool_name,
                            record.tenant_id,
                            record.user_id,
                            json.dumps(record.arguments, ensure_ascii=False, default=str),
                            record.side_effects,
                            record.success,
                            record.error_type,
                            record.error_code,
                            record.error_message,
                            record.duration_ms,
                            record.replayed,
                            record.created_at,
                        ),
                    )
                conn.commit()
        except Exception:
            # 审计失败不应阻断工具执行主流程，降级为日志
            import logging

            logging.getLogger(__name__).exception("audit_record_failed tool=%s", record.tool_name)

    def query(
        self,
        *,
        trace_id: Optional[str] = None,
        tool_name: Optional[str] = None,
        tenant_id: Optional[str] = None,
        limit: int = 100,
    ) -> List[ToolCallAuditRecord]:
        conditions: List[str] = []
        params: List[Any] = []
        if trace_id:
            conditions.append("trace_id = %s")
            params.append(trace_id)
        if tool_name:
            conditions.append("tool_name = %s")
            params.append(tool_name)
        if tenant_id:
            conditions.append("tenant_id = %s")
            params.append(tenant_id)

        where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
        sql = f"SELECT * FROM tool_call_audit {where} ORDER BY created_at DESC LIMIT %s"
        params.append(limit)

        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(sql, params)
                rows = cur.fetchall()

        return [
            ToolCallAuditRecord(
                audit_id=r["audit_id"],
                trace_id=r["trace_id"],
                tool_call_id=r["tool_call_id"],
                tool_name=r["tool_name"],
                tenant_id=r["tenant_id"],
                user_id=r["user_id"],
                arguments=r["arguments"] if isinstance(r["arguments"], dict) else json.loads(r["arguments"]),
                side_effects=r["side_effects"],
                success=r["success"],
                error_type=r["error_type"],
                error_code=r["error_code"],
                error_message=r["error_message"],
                duration_ms=float(r["duration_ms"]),
                replayed=r["replayed"],
                created_at=float(r["created_at"]),
            )
            for r in rows
        ]


class ElasticsearchAuditStore(AuditStore):
    """Elasticsearch 审计存储（依赖服务占位实现）.

    依赖 elasticsearch 官方客户端与 ES_HOSTS 配置；ES_HOSTS 留空时初始化报错，
    由 build_audit_store_from_settings 统一降级为内存实现，服务补充配置后自动启用。
    """

    def __init__(
        self,
        hosts: Optional[str] = None,
        index: Optional[str] = None,
        api_key: Optional[str] = None,
    ):
        from react_ai.config.settings import settings

        self.hosts = hosts or settings.observability.es_hosts
        self.index = index or settings.observability.es_index
        self.api_key = api_key if api_key is not None else settings.observability.es_api_key
        if not self.hosts:
            raise ValueError(
                "ElasticsearchAuditStore 需要配置 ES_HOSTS（当前留空，请补充依赖服务地址）"
            )
        from elasticsearch import Elasticsearch

        kwargs: Dict[str, Any] = {"hosts": self.hosts}
        if self.api_key:
            kwargs["api_key"] = self.api_key
        self._client = Elasticsearch(**kwargs)

    def record(self, record: ToolCallAuditRecord) -> None:
        try:
            self._client.index(index=self.index, id=record.audit_id, document=record.model_dump())
        except Exception:
            # 审计失败不阻断主流程，与 PostgresAuditStore 行为一致
            import logging

            logging.getLogger(__name__).exception("es_audit_record_failed tool=%s", record.tool_name)

    def query(
        self,
        *,
        trace_id: Optional[str] = None,
        tool_name: Optional[str] = None,
        tenant_id: Optional[str] = None,
        limit: int = 100,
    ) -> List[ToolCallAuditRecord]:
        filters: List[Dict[str, Any]] = []
        if trace_id:
            filters.append({"term": {"trace_id": trace_id}})
        if tool_name:
            filters.append({"term": {"tool_name": tool_name}})
        if tenant_id:
            filters.append({"term": {"tenant_id": tenant_id}})
        resp = self._client.search(
            index=self.index,
            query={"bool": {"filter": filters}} if filters else {"match_all": {}},
            sort=[{"created_at": "desc"}],
            size=limit,
        )
        return [ToolCallAuditRecord(**hit["_source"]) for hit in resp["hits"]["hits"]]


def build_audit_store_from_settings() -> AuditStore:
    """按 settings.observability.audit_backend 构建审计存储.

    - memory：内存（开发/测试）
    - postgres：PostgreSQL 落库（表自动创建；DB 不可用降级内存）
    - elasticsearch：ES 落库（ES_HOSTS 留空或客户端缺失时降级内存）
    任何后端初始化失败均降级为 InMemoryAuditStore，不阻断服务启动。
    """
    import logging

    from react_ai.config.settings import settings

    backend = (settings.observability.audit_backend or "memory").lower()
    log = logging.getLogger(__name__)

    if backend == "postgres":
        try:
            store = PostgresAuditStore()
            store.ensure_table()
            return store
        except Exception as exc:
            log.warning("audit backend [postgres] unavailable, fallback to memory: %s", exc)
    elif backend == "elasticsearch":
        try:
            return ElasticsearchAuditStore()
        except Exception as exc:
            log.warning("audit backend [elasticsearch] unavailable, fallback to memory: %s", exc)
    return InMemoryAuditStore()


_default_audit_store: Optional[AuditStore] = None


def get_audit_store() -> AuditStore:
    """进程级单例：默认内存实现，可通过 set_audit_store 替换为 PostgreSQL."""
    global _default_audit_store
    if _default_audit_store is None:
        _default_audit_store = InMemoryAuditStore()
    return _default_audit_store


def set_audit_store(store: AuditStore) -> None:
    """替换全局审计存储（如切换到 PostgreSQL 实现）."""
    global _default_audit_store
    _default_audit_store = store
