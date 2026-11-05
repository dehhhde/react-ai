"""OpenTelemetry 分布式链路追踪集成.

设计要点：
- 提供 async context manager `start_span`，包裹工具执行，自动采集耗时与属性；
- 默认内存实现（记录 span 数据，供测试/调试）；
- 当 settings.observability.otlp_endpoint 配置时，可对接真实 OTel SDK（可选依赖）；
- 不阻断主流程：OTel 导出失败不影响工具执行。
"""
from __future__ import annotations

import time
from contextlib import asynccontextmanager, contextmanager
from typing import Any, Dict, List, Optional

from react_ai.config.settings import settings


class SpanRecord:
    """一次 span 的完整记录（内存实现用）."""

    def __init__(
        self,
        name: str,
        attributes: Optional[Dict[str, Any]] = None,
        parent_span_id: Optional[str] = None,
    ):
        import uuid

        self.span_id = uuid.uuid4().hex
        self.trace_id = (attributes or {}).get("trace_id") or uuid.uuid4().hex
        self.parent_span_id = parent_span_id
        self.name = name
        self.attributes: Dict[str, Any] = dict(attributes or {})
        self.start_time: float = time.time()
        self.end_time: Optional[float] = None
        self.status: str = "ok"  # ok | error
        self.error_message: Optional[str] = None

    def end(self, status: str = "ok", error_message: Optional[str] = None) -> None:
        self.end_time = time.time()
        self.status = status
        self.error_message = error_message

    @property
    def duration_ms(self) -> float:
        end = self.end_time or time.time()
        return (end - self.start_time) * 1000

    def to_dict(self) -> Dict[str, Any]:
        return {
            "span_id": self.span_id,
            "trace_id": self.trace_id,
            "parent_span_id": self.parent_span_id,
            "name": self.name,
            "attributes": self.attributes,
            "start_time": self.start_time,
            "end_time": self.end_time,
            "duration_ms": round(self.duration_ms, 2),
            "status": self.status,
            "error_message": self.error_message,
        }


class AgentTracer:
    """链路追踪器：支持内存记录 + 可选 OTel 导出."""

    def __init__(self, service_name: str = "react-ai"):
        self.service_name = service_name
        self._spans: List[SpanRecord] = []
        self._otel_tracer = None
        self._init_otel()

    def _init_otel(self) -> None:
        """可选对接 OpenTelemetry SDK；未安装或未配置 endpoint 时静默跳过."""
        endpoint = settings.observability.otlp_endpoint
        if not endpoint:
            return
        try:
            from opentelemetry import trace
            from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
            from opentelemetry.sdk.trace import TracerProvider
            from opentelemetry.sdk.trace.export import BatchSpanProcessor

            provider = TracerProvider()
            provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(endpoint=endpoint)))
            trace.set_tracer_provider(provider)
            self._otel_tracer = trace.get_tracer(self.service_name)
        except Exception:
            # OTel 依赖缺失或配置错误时降级为内存实现，不阻断主流程
            self._otel_tracer = None

    @asynccontextmanager
    async def start_span(self, name: str, attributes: Optional[Dict[str, Any]] = None):
        """开启一个 span，自动记录耗时、状态与异常.

        用法：
            async with tracer.start_span("tool.call", {"tool": name, ...}) as span:
                ...
                span.set_attribute("result", "ok")
        """
        record = SpanRecord(name=name, attributes=attributes)

        otel_span = None
        if self._otel_tracer is not None:
            try:
                otel_span = self._otel_tracer.start_span(name, attributes=attributes or {})
                otel_span.__enter__()
            except Exception:
                otel_span = None

        class _SpanProxy:
            """代理对象，暴露 set_attribute 给业务代码."""

            def __init__(self, rec: SpanRecord):
                self._rec = rec

            def set_attribute(self, key: str, value: Any) -> None:
                self._rec.attributes[key] = value
                if otel_span is not None:
                    try:
                        otel_span.set_attribute(key, value)
                    except Exception:
                        pass

        proxy = _SpanProxy(record)

        try:
            yield proxy
            record.end(status="ok")
        except Exception as exc:
            record.end(status="error", error_message=str(exc))
            if otel_span is not None:
                try:
                    otel_span.record_exception(exc)
                    otel_span.set_status(__import__("opentelemetry.trace", fromlist=["Status", "StatusCode"]).Status(
                        __import__("opentelemetry.trace", fromlist=["StatusCode"]).StatusCode.ERROR
                    ))
                except Exception:
                    pass
            raise
        finally:
            if otel_span is not None:
                try:
                    otel_span.__exit__(None, None, None)
                except Exception:
                    pass
            self._spans.append(record)
            # 防止内存无限增长，最多保留最近 10000 条
            if len(self._spans) > 10000:
                self._spans = self._spans[-10000:]

    @contextmanager
    def span(self, name: str, attributes: Optional[Dict[str, Any]] = None):
        """同步版 span 上下文管理器（供 prompt.render 等同步代码段接入链路追踪）."""
        record = SpanRecord(name=name, attributes=attributes)

        class _SyncSpanProxy:
            def __init__(self, rec: SpanRecord):
                self._rec = rec

            def set_attribute(self, key: str, value: Any) -> None:
                self._rec.attributes[key] = value

        proxy = _SyncSpanProxy(record)
        try:
            yield proxy
            record.end(status="ok")
        except Exception as exc:
            record.end(status="error", error_message=str(exc))
            raise
        finally:
            self._spans.append(record)
            if len(self._spans) > 10000:
                self._spans = self._spans[-10000:]

    def recent_spans(self, limit: int = 100) -> List[Dict[str, Any]]:
        """返回最近的 span 记录（供调试/管理面查询）."""
        return [s.to_dict() for s in self._spans[-limit:]]

    def reset(self) -> None:
        self._spans.clear()


tracer = AgentTracer()
