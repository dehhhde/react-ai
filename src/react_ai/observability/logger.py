"""结构化 JSON 日志器 (内置 Trace-ID 与 Session-ID 关联).

每条日志一行 JSON，便于 ELK/Loki 采集与字段检索；`bind()` 用于把 thread_id 等
关联字段绑定到 logger 实例上，避免每次调用重复传参。
"""
import json
import logging
import sys
import time
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from react_ai.config import settings

_LEVELS = {
    "DEBUG": logging.DEBUG,
    "INFO": logging.INFO,
    "WARNING": logging.WARNING,
    "ERROR": logging.ERROR,
    "CRITICAL": logging.CRITICAL,
}


class StructuredLogger:
    def __init__(self, name: str = "react-ai", **bound_fields: Any):
        self.name = name
        self._bound: Dict[str, Any] = dict(bound_fields)
        self._logger = logging.getLogger(name)
        if not self._logger.handlers:
            handler = logging.StreamHandler(sys.stderr)  # 日志走 stderr，避免污染脚本输出到 stdout 的 JSON 结果
            handler.setFormatter(logging.Formatter("%(message)s"))
            self._logger.addHandler(handler)
        self._logger.setLevel(_LEVELS.get(str(settings.observability.log_level).upper(), logging.INFO))

    def bind(self, **fields: Any) -> "StructuredLogger":
        """返回携带固定上下文字段的子 logger（共享同一底层 handler）。"""
        child = StructuredLogger(self.name, **{**self._bound, **fields})
        child._logger = self._logger
        return child

    def log(self, level: str, message: str, context: Optional[Dict[str, Any]] = None, **kwargs):
        payload = {
            "timestamp": time.time(),
            "time": datetime.fromtimestamp(time.time(), tz=timezone.utc).isoformat(timespec="milliseconds"),
            "level": level.upper(),
            "message": message,
            "logger": self.name,
            "context": context or {},
            "extra": {**self._bound, **kwargs},
        }
        self._logger.log(_LEVELS.get(level.upper(), logging.INFO), json.dumps(payload, ensure_ascii=False, default=str))

    def debug(self, message: str, **kwargs):
        self.log("DEBUG", message, **kwargs)

    def info(self, message: str, **kwargs):
        self.log("INFO", message, **kwargs)

    def warning(self, message: str, **kwargs):
        self.log("WARNING", message, **kwargs)

    def error(self, message: str, **kwargs):
        self.log("ERROR", message, **kwargs)


logger = StructuredLogger()
