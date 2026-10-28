"""指标度量收集 (Token 消耗、响应延迟、工具调用异常率)."""
from typing import Dict


class MetricsCollector:
    def __init__(self):
        self.total_tokens: int = 0
        self.total_requests: int = 0
        self.tool_call_counts: Dict[str, int] = {}
        self.tool_error_counts: Dict[str, int] = {}

    def record_request(self, tokens: int = 0):
        self.total_requests += 1
        self.total_tokens += tokens

    def record_tool_call(self, tool_name: str, success: bool):
        self.tool_call_counts[tool_name] = self.tool_call_counts.get(tool_name, 0) + 1
        if not success:
            self.tool_error_counts[tool_name] = self.tool_error_counts.get(tool_name, 0) + 1

    def snapshot(self) -> dict:
        return {
            "total_requests": self.total_requests,
            "total_tokens": self.total_tokens,
            "tool_call_counts": dict(self.tool_call_counts),
            "tool_error_counts": dict(self.tool_error_counts),
        }


metrics = MetricsCollector()
