"""OpenTelemetry 分布式链路追踪集成."""
from typing import Optional
from contextlib import asynccontextmanager


class AgentTracer:
    def __init__(self, service_name: str = "react-ai"):
        self.service_name = service_name

    @asynccontextmanager
    async def start_span(self, name: str, attributes: Optional[dict] = None):
        # 生产对接 opentelemetry.trace
        span_data = {"name": name, "attributes": attributes or {}}
        try:
            yield span_data
        finally:
            pass


tracer = AgentTracer()
