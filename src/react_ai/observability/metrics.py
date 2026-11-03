"""指标度量收集 (带标签计数器 / 直方图 / Prometheus 文本导出).

用法（业务侧只依赖 metrics 单例）：

    metrics.inc("intent_extraction_total", {"source": "llm"})
    metrics.observe("node_latency_seconds", elapsed, {"node": "intent_recognition"})
    metrics.record_tool_call(tool_name="query_order", tenant_id="t1",
                             success=False, error_type="business",
                             error_code="order_not_found", duration_ms=12.3)

工具调用按 tool/tenant/error_type/error_code 维度记录，统一并入通用序列：
计数器 react_ai_tool_calls_total 与直方图 react_ai_tool_call_duration_ms。
snapshot() 供健康检查与脚本自证，to_prometheus_text()/to_prometheus() 供
/metrics 端点抓取（含熔断状态 gauge）。
"""
import threading
from typing import Dict, List, Optional, Tuple

# 直方图保留的样本上限：仅用于分位数估算，count/sum 仍全量累计，长期运行内存有界。
HISTOGRAM_SAMPLE_LIMIT = 1024
QUANTILES = (0.5, 0.95, 0.99)


def _labels_key(labels: Optional[Dict[str, str]]) -> str:
    if not labels:
        return ""
    # Prometheus 文本格式要求标签值加引号，同一格式也用作进程内的序列标识
    return ",".join(f'{k}="{_escape_label(str(labels[k]))}"' for k in sorted(labels))


def _escape_label(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def _render_name(name: str, key: str) -> str:
    return f"{name}{{{key}}}" if key else name


class MetricsCollector:
    """指标收集器：通用带标签序列 + 工具调用 tool/tenant/error_type 维度统计."""

    def __init__(self):
        self._lock = threading.Lock()
        self.total_tokens: int = 0
        self.total_requests: int = 0
        self.tool_call_counts: Dict[str, int] = {}
        self.tool_error_counts: Dict[str, int] = {}
        self._counters: Dict[str, Dict[str, float]] = {}
        self._counter_labels: Dict[str, Dict[str, Dict[str, str]]] = {}
        self._histograms: Dict[str, Dict[str, Dict[str, object]]] = {}

    # ------------------------------------------------------------------ 兼容旧接口
    def record_request(self, tokens: int = 0):
        with self._lock:
            self.total_requests += 1
            self.total_tokens += tokens

    def record_tool_call(
        self,
        tool_name: str,
        tenant_id: str = "default",
        success: bool = True,
        error_type: str = "none",
        error_code: str = "none",
        duration_ms: float = 0.0,
    ) -> None:
        """记录一次工具调用.

        同时写入三处：
        - 旧版兼容字段 tool_call_counts / tool_error_counts
        - 计数器 react_ai_tool_calls_total{tool,tenant,error_type,error_code}
        - 直方图 react_ai_tool_call_duration_ms{tool,tenant}

        Args:
            tool_name: 工具名
            tenant_id: 租户ID
            success: 是否成功
            error_type: 错误大类（schema/business/auth/external），成功时为 "none"
            error_code: 具体错误码（rate_limited/circuit_open/tool_timeout 等），成功时为 "none"
            duration_ms: 耗时毫秒
        """
        with self._lock:
            self.tool_call_counts[tool_name] = self.tool_call_counts.get(tool_name, 0) + 1
            if not success:
                self.tool_error_counts[tool_name] = self.tool_error_counts.get(tool_name, 0) + 1
        self.inc("react_ai_tool_calls_total", {
            "tool": tool_name,
            "tenant": tenant_id,
            "error_type": error_type,
            "error_code": error_code,
        })
        self.observe("react_ai_tool_call_duration_ms", duration_ms, {
            "tool": tool_name,
            "tenant": tenant_id,
        })

    # ------------------------------------------------------------------ 计数器
    def inc(self, name: str, labels: Optional[Dict[str, str]] = None, value: float = 1.0):
        key = _labels_key(labels)
        with self._lock:
            bucket = self._counters.setdefault(name, {})
            bucket[key] = bucket.get(key, 0.0) + value
            self._counter_labels.setdefault(name, {}).setdefault(key, dict(labels or {}))

    def counter(self, name: str, labels: Optional[Dict[str, str]] = None) -> float:
        key = _labels_key(labels)
        with self._lock:
            return self._counters.get(name, {}).get(key, 0.0)

    # ------------------------------------------------------------------ 直方图
    def observe(self, name: str, value: float, labels: Optional[Dict[str, str]] = None):
        key = _labels_key(labels)
        with self._lock:
            bucket = self._histograms.setdefault(name, {})
            series = bucket.get(key)
            if series is None:
                series = {"count": 0, "sum": 0.0, "samples": [], "labels": dict(labels or {})}
                bucket[key] = series
            series["count"] += 1
            series["sum"] += float(value)
            samples: List[float] = series["samples"]  # type: ignore[assignment]
            samples.append(float(value))
            if len(samples) > HISTOGRAM_SAMPLE_LIMIT:  # 保留最近样本，避免无界增长
                del samples[: len(samples) - HISTOGRAM_SAMPLE_LIMIT]

    def histogram(self, name: str, labels: Optional[Dict[str, str]] = None) -> Dict[str, object]:
        key = _labels_key(labels)
        with self._lock:
            series = self._histograms.get(name, {}).get(key)
            if not series:
                return {"count": 0, "sum": 0.0, "avg": None, "quantiles": {}}
            return _summarize(int(series["count"]), float(series["sum"]), sorted(series["samples"]))  # type: ignore[arg-type]

    def histogram_series(self, name: str) -> List[Tuple[Dict[str, str], Dict[str, object]]]:
        """按标签展开某直方图的全部序列，便于报表/断言。"""
        with self._lock:
            labeled = [(dict(series["labels"]), int(series["count"]), float(series["sum"]), sorted(series["samples"]))  # type: ignore[arg-type]
                       for series in self._histograms.get(name, {}).values()]
        return [(labels, _summarize(count, total, samples)) for labels, count, total, samples in labeled]

    # ------------------------------------------------------------------ 导出
    def snapshot(self) -> dict:
        with self._lock:
            counters = {
                name: [{"labels": labels, "value": self._counters[name][key]}
                       for key, labels in self._counter_labels.get(name, {}).items()]
                for name in self._counters
            }
            histograms = {
                name: [{"labels": dict(series["labels"]),
                        **_summarize(int(series["count"]), float(series["sum"]), sorted(series["samples"]))}  # type: ignore[arg-type]
                       for series in bucket.values()]
                for name, bucket in self._histograms.items()
            }
        return {
            "total_requests": self.total_requests,
            "total_tokens": self.total_tokens,
            "tool_call_counts": dict(self.tool_call_counts),
            "tool_error_counts": dict(self.tool_error_counts),
            "counters": counters,
            "histograms": histograms,
        }

    def to_prometheus_text(self) -> str:
        """Prometheus 文本格式：计数器原样，直方图按 summary 类型导出分位数，末尾附熔断状态 gauge。"""
        snap = self.snapshot()
        lines: List[str] = [
            "# HELP react_ai_total_requests Total agent requests.",
            "# TYPE react_ai_total_requests counter",
            f"react_ai_total_requests {snap['total_requests']}",
            "# HELP react_ai_total_tokens Total tokens consumed.",
            "# TYPE react_ai_total_tokens counter",
            f"react_ai_total_tokens {snap['total_tokens']}",
        ]
        for name, series_list in snap["counters"].items():
            lines.append(f"# TYPE {name} counter")
            for item in series_list:
                lines.append(f"{_render_name(name, _labels_key(item['labels']))} {item['value']}")
        for name, series_list in snap["histograms"].items():
            lines.append(f"# TYPE {name} summary")
            for series in series_list:
                key = _labels_key(series["labels"])
                for q, value in series["quantiles"].items():
                    label = f'{key},quantile="{q}"' if key else f'quantile="{q}"'
                    lines.append(f"{name}{{{label}}} {value}")
                lines.append(f"{_render_name(f'{name}_sum', key)} {series['sum']}")
                lines.append(f"{_render_name(f'{name}_count', key)} {series['count']}")
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

    def to_prometheus(self) -> str:
        """to_prometheus_text 的别名（observability 路由沿用该入口名）。"""
        return self.to_prometheus_text()

    def reset(self):
        """清空全部指标（单测与离线演示用，生产不调用）。"""
        with self._lock:
            self.total_tokens = 0
            self.total_requests = 0
            self.tool_call_counts.clear()
            self.tool_error_counts.clear()
            self._counters.clear()
            self._counter_labels.clear()
            self._histograms.clear()


def _summarize(count: int, total: float, ordered_samples: List[float]) -> Dict[str, object]:
    return {
        "count": count,
        "sum": round(total, 6),
        "avg": round(total / count, 6) if count else None,
        "quantiles": {q: _quantile(ordered_samples, q) for q in QUANTILES},
    }


def _quantile(ordered: List[float], q: float) -> Optional[float]:
    if not ordered:
        return None
    idx = min(len(ordered) - 1, max(0, int(round(q * (len(ordered) - 1)))))
    return round(ordered[idx], 6)


metrics = MetricsCollector()
