from typing import Any
"""安全 JSON 序列化工具."""
import json
from pydantic import BaseModel


def to_json_safe(obj: Any) -> str:
    if isinstance(obj, BaseModel):
        return obj.model_dump_json()
    return json.dumps(obj, default=str, ensure_ascii=False)
