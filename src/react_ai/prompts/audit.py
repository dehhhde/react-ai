"""提示词渲染审计存储（差距分析 2.7 配套基础设施）.

每次 render 的 PromptResult 快照（变量已脱敏）落库，可按 trace_id 回查
"这次请求用了哪版提示词、注入了哪些变量"。默认内存环形存储，
可选配置 JSONL 文件路径持久化（与 PG 审计后端同构，后续可替换实现）。
"""
from __future__ import annotations

import json
import logging
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)


class PromptAuditStore:
    """渲染审计存储：内存环形 + 可选 JSONL 落盘."""

    def __init__(self, max_records: int = 5000, file_path: str | Path | None = None):
        self._lock = threading.Lock()
        self._records: list[dict[str, Any]] = []
        self._max_records = max_records
        self._file_path = Path(file_path) if file_path else None

    def record(self, entry: dict[str, Any]) -> None:
        """写入一条渲染审计记录（entry 内变量须已脱敏）."""
        with self._lock:
            self._records.append(entry)
            if len(self._records) > self._max_records:
                self._records = self._records[-self._max_records:]
        if self._file_path:
            try:
                self._file_path.parent.mkdir(parents=True, exist_ok=True)
                with self._file_path.open("a", encoding="utf-8") as f:
                    f.write(json.dumps(entry, ensure_ascii=False, default=str) + "\n")
            except OSError as exc:
                logger.warning("prompt_audit_write_failed path=%s error=%s", self._file_path, exc)

    def get_by_trace_id(self, trace_id: str) -> list[dict[str, Any]]:
        """按 trace_id 回查历史渲染快照."""
        with self._lock:
            return [r for r in self._records if r.get("trace_id") == trace_id]

    def recent(self, limit: int = 50) -> list[dict[str, Any]]:
        with self._lock:
            return list(self._records[-limit:])

    def clear(self) -> None:
        with self._lock:
            self._records.clear()


def build_audit_entry(
    result: Any,
    locale: Optional[str] = None,
    guardrail_hits: Optional[list[str]] = None,
) -> dict[str, Any]:
    """从 PromptResult 构造审计条目（variables_used 已在渲染阶段脱敏）."""
    return {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "trace_id": result.trace_id,
        "prompt_name": result.prompt_name,
        "version": result.version,
        "locale": locale,
        "variables_used": result.variables_used,
        "render_duration_ms": result.render_duration_ms,
        "token_count": getattr(result, "token_count", 0),
        "message_roles": [m.role for m in result.messages],
        "guardrail_hits": guardrail_hits or [],
    }


prompt_audit_store = PromptAuditStore()
