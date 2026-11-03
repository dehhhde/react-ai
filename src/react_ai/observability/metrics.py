"""指标度量收集 (Token 消耗、响应延迟、工具调用异常率).

维度设计：
- tool_name：工具名
- tenant_id：租户
- error_type：错误大类（schema/business/auth/external）或 "none"（成功）
- error_code：具体错误码（限流/熔断/超时等）

提供 Prometheus 文本格式导出，便于对接 /metrics 端点。
"""
from __future__ import annotations

import threading
import time
from collections import defaultdict
from typing import Dict, List, Optional, Tuple


class _Counter:
    """线程安全计数器."""

    def __init__(self):
        self._value: float = 0.0
        self._lock = threading.Lock()

    def inc(self, amount: float = 1.0) -> None:
        with self._lock:
            self._value += amount

    @property
    def value(self) -> float:
        with self._lock:
            return self._value


class _Histogram:
    """简易直方图：记录 count + sum，按分位数近似（P50/P90/P99 用排序样本）."""

    def __init__(self, max_samples: int = 10000):
        self.count = 0
        self.sum = 0.0
        self._samples: List[float] = []
        self._max_samples = max_samples
        self._lock = threading.Lock()

    def observe(self, value: float) -> None:
        with self._lock:
            self.count += 1
            self.sum += value
            if len(self._samples) < self._max_samples:
                self._samples.append(value)
            else:
                # 环形覆盖，保持固定样本量
                self._samples[self.count % self._max_samples] = value

    def quantile(self, q: float) -> float:
        with self._lock:
            if not self._samples:
                return 0.0
            sorted_samples = sorted(self._samples)
            idx = min(int(q * len(sorted_samples)), len(sorted_samples) - 1)
            return sorted_samples[idx]

    @property
    def avg(self) -> float:
        return self.sum / self.count if self.count else 0.0


class MetricsCollector:
    """工具调用指标收集器：按 tool/tenant/error_type 维度统计."""

    def __init__(self):
        self._lock = threading.Lock()
        # 计数器：tool_calls_total{tool,tenant,error_type,error_code}
        self._calls: Dict[Tuple[str, str, str, str], _Counter] = defaultdict(_Counter)
        # 直方图：tool_call_duration_ms{tool,tenant}
        self._durations: Dict[Tuple[str, str], _Histogram] = defaultdict(_Histogram)
        # 全局 token 计数
        self.total_tokens: int = 0
        self.total_requests: int = 0

    def record_request(self, tokens: int = 0) -> None:
        with self._lock:
            self.total_requests += 1
            self.total_tokens += tokens

    def record_tool_call(
        self,
        tool_name: str,
        tenant_id: str,
        success: bool,
        error_type: str = "none",
        error_code: str = "none",
        duration_ms: float = 0.0,
    ) -> None:
        """记录一次工具调用.

        Args:
            tool_name: 工具名
            tenant_id: 租户ID
            success: 是否成功
            error_type: 错误大类（schema/business/auth/external），成功时为 "none"
            error_code: 具体错误码（rate_limited/circuit_open/tool_timeout 等），成功时为 "none"
            duration_ms: 耗时毫秒
        """
        key = (tool_name, tenant_id, error_type, error_code)
        self._calls[key].inc(1.0)
        self._durations[(tool_name, tenant_id)].observe(duration_ms)

    def snapshot(self) -> dict:
        """返回结构化快照（JSON 友好）."""
        calls: Dict[str, int] = {}
        for (tool, tenant, etype, ecode), counter in self._calls.items():
            label = f"tool={tool},tenant={tenant},error_type={etype},error_code={ecode}"
            calls[label] = int(counter.value)

        durations: Dict[str, dict] = {}
        for (tool, tenant), hist in self._durations.items():
            label = f"tool={tool},tenant={tenant}"
            durations[label] = {
                "count": hist.count,
                "avg_ms": round(hist.avg, 2),
                "p50_ms": round(hist.quantile(0.5), 2),
                "p90_ms": round(hist.quantile(0.9), 2),
                "p99_ms": round(hist.quantile(0.99), 2),
            }

        return {
            "total_requests": self.total_requests,
            "total_tokens": self.total_tokens,
            "tool_calls": calls,
            "tool_call_durations": durations,
        }

    def to_prometheus(self) -> str:
        """导出 Prometheus 文本格式，供 /metrics 端点直接返回."""
        lines: List[str] = []
        lines.append("# HELP react_ai_tool_calls_total Total tool calls by tool/tenant/error_type/error_code")
        lines.append("# TYPE react_ai_tool_calls_total counter")
        for (tool, tenant, etype, ecode), counter in self._calls.items():
            lines.append(
                f'react_ai_tool_calls_total{{tool="{tool}",tenant="{tenant}",'
                f'error_type="{etype}",error_code="{ecode}"}} {int(counter.value)}'
            )

        lines.append("# HELP react_ai_tool_call_duration_ms Tool call duration in milliseconds")
        lines.append("# TYPE react_ai_tool_call_duration_ms summary")
        for (tool, tenant), hist in self._durations.items():
            lines.append(
                f'react_ai_tool_call_duration_ms_count{{tool="{tool}",tenant="{tenant}"}} {hist.count}'
            )
            lines.append(
                f'react_ai_tool_call_duration_ms_sum{{tool="{tool}",tenant="{tenant}"}} {round(hist.sum, 2)}'
            )

        # 熔断状态 gauge：0=closed 1=half-open 2=open
        try:
            from react_ai.tools.resilience import get_circuit_breaker_registry

            state_map = {"closed": 0, "half-open": 1, "open": 2}
            lines.append("# HELP react_ai_circuit_breaker_state Circuit breaker state per tool (0=closed, 1=half-open, 2=open)")
            lines.append("# TYPE react_ai_circuit_breaker_state gauge")
            for tool_name, cb in get_circuit_breaker_registry().all().items():
                lines.append(
                    f'react_ai_circuit_breaker_state{{tool="{tool_name}"}} {state_map.get(cb.state, 0)}'
                )
        except Exception:
            pass

        return "\n".join(lines) + "\n"

    def reset(self) -> None:
        with self._lock:
            self._calls.clear()
            self._durations.clear()
            self.total_tokens = 0
            self.total_requests = 0


metrics = MetricsCollector()
