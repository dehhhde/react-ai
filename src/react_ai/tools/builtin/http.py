"""HTTP API 安全调用工具."""
from typing import Any, Dict
import httpx
from react_ai.tools.base import BaseTool, ToolContext


class HttpTool(BaseTool):
    name = "http_request"
    description = "发起 RESTful HTTP 请求"
    side_effects = "none"
    tags = ["network", "http"]

    @property
    def input_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "url": {"type": "string", "description": "目标 URL 地址"},
                "method": {"type": "string", "description": "HTTP 请求方法 (GET, POST 等)"},
                "headers": {"type": "object", "description": "自定义 HTTP 请求头"},
                "json": {"type": "object", "description": "请求 JSON 载荷"},
            },
            "required": ["url"],
        }

    def execute(self, arguments: dict, ctx: ToolContext) -> Any:
        url = arguments.get("url", "")
        method = arguments.get("method", "GET").upper()
        headers = arguments.get("headers", {})
        json_data = arguments.get("json", None)

        with httpx.Client(timeout=20.0) as client:
            resp = client.request(method=method, url=url, headers=headers, json=json_data)
            return {"status_code": resp.status_code, "text": resp.text[:2000]}
