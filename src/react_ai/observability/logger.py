"""结构化 JSON 日志器 (内置 Trace-ID 与 Session-ID 关联)."""
import json
import logging
import time
from typing import Any, Dict, Optional


class StructuredLogger:
    def __init__(self, name: str = "react-ai"):
        self._logger = logging.getLogger(name)
        if not self._logger.handlers:
            handler = logging.StreamHandler()
            handler.setFormatter(logging.Formatter("%(message)s"))
            self._logger.addHandler(handler)
            self._logger.setLevel(logging.INFO)

    def log(self, level: str, message: str, context: Optional[Dict[str, Any]] = None, **kwargs):
        payload = {
            "timestamp": time.time(),
            "level": level.upper(),
            "message": message,
            "context": context or {},
            "extra": kwargs,
        }
        self._logger.info(json.dumps(payload, ensure_ascii=False))

    def info(self, message: str, **kwargs):
        self.log("INFO", message, **kwargs)

    def warning(self, message: str, **kwargs):
        self.log("WARNING", message, **kwargs)

    def error(self, message: str, **kwargs):
        self.log("ERROR", message, **kwargs)


logger = StructuredLogger()
