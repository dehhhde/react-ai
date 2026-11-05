"""指标度量收集 (带标签计数器 / 直方图 / Prometheus 文本导出).

用法（业务侧只依赖 metrics 单例）：

    metrics.inc("intent_extraction_total", {"source": "llm"})
    metrics.observe("node_latency_seconds", elapsed, {"node": "intent_recognition"})
    metrics.record_tool_call(tool_name="query_order", tenant_id="t1",
                             success=False, error_type="business",
                             error_code="order_not_found", duration_ms=12.3)

工具调用按 tool/tenant/error_type/error_code 维度记录，统一并入通用序列：
计数器 react_ai_tool_calls_total 与直方图 react_ai_tool_call_duration_ms。
提示词渲染（§2.7）与记忆路径（§2.8）另按 name/version、op/backend/tenant/result
维度记录，供按版本归因质量成本、按后端定位召回与容量问题。
snapshot() 供健康检查与脚本自证，to_prometheus_text()/to_prometheus() 供
/metrics 端点抓取（含熔断状态 gauge）。
"""
import threading
from collections import defaultdict
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


class _Counter:
    """线程安全计数器（提示词/记忆等专用维度使用）."""

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
    """简易直方图：记录 count + sum，按排序样本估算分位数（P50/P90/P99）."""

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
    """指标收集器：通用带标签序列 + 工具调用 / 提示词渲染 / 记忆路径专用维度."""

    def __init__(self):
        self._lock = threading.Lock()
        # 通用带标签计数器/直方图：name -> {labels_key -> value/series}
        self._counters: Dict[str, Dict[str, float]] = {}
        self._counter_labels: Dict[str, Dict[str, Dict[str, str]]] = {}
        self._histograms: Dict[str, Dict[str, Dict[str, object]]] = {}
        # 提示词渲染计数：prompt_renders_total{name,version}
        self._prompt_renders: Dict[Tuple[str, str], _Counter] = defaultdict(_Counter)
        # 提示词渲染耗时：prompt_render_duration_ms{name,version}
        self._prompt_durations: Dict[Tuple[str, str], _Histogram] = defaultdict(_Histogram)
        # 提示词渲染 token 累计：prompt_render_tokens{name,version}
        self._prompt_tokens: Dict[Tuple[str, str], _Counter] = defaultdict(_Counter)
        # 记忆操作计数：memory_ops_total{op,backend,tenant,result}（差距分析 2.8）
        self._memory_ops: Dict[Tuple[str, str, str, str], _Counter] = defaultdict(_Counter)
        # 记忆操作耗时：memory_op_duration_ms{op,backend}
        self._memory_durations: Dict[Tuple[str, str], _Histogram] = defaultdict(_Histogram)
        # 记忆操作处理条目数：memory_items_total{op,backend}
        self._memory_items: Dict[Tuple[str, str], _Counter] = defaultdict(_Counter)
        # 记忆淘汰条目数：memory_evictions_total{backend}
        self._memory_evictions: Dict[str, _Counter] = defaultdict(_Counter)
        # 存储后端健康度：1=最近一次操作成功，0=最近一次操作异常
        self._memory_health: Dict[str, int] = {}
        # 全局 token 计数与旧版兼容字段
        self.total_tokens: int = 0
        self.total_requests: int = 0
        self.tool_call_counts: Dict[str, int] = {}
        self.tool_error_counts: Dict[str, int] = {}

    # ------------------------------------------------------------------ 提示词渲染
    def record_prompt_render(
        self,
        prompt_name: str,
        version: str,
        tokens: int = 0,
        duration_ms: float = 0.0,
    ) -> None:
        """记录一次提示词渲染（次数/耗时/token 消耗），供按版本归因质量与成本."""
        key = (prompt_name, version)
        self._prompt_renders[key].inc(1.0)
        self._prompt_durations[key].observe(duration_ms)
        if tokens:
            self._prompt_tokens[key].inc(float(tokens))

    # ------------------------------------------------------------------ 兼容旧接口
    def record_request(self, tokens: int = 0):
        with self._lock:
            self.total_requests += 1
            self.total_tokens += tokens

    def record_memory_op(
        self,
        op: str,
        backend: str,
        tenant_id: str = "",
        result: str = "ok",
        duration_ms: float = 0.0,
        items: int = 0,
    ) -> None:
        """记录一次记忆操作（差距分析 2.8）.

        Args:
            op: 操作名，如 load/save/read/write/delete/clear_session/search
            backend: 存储后端标识（InMemoryMemory / RedisMemory / vector 索引名等）
            tenant_id: 租户标识（缺失记为空串，不会产生跨租户泄漏）
            result: ok / error / hit / miss；hit/miss 用于计算召回命中率
            duration_ms: 操作耗时（毫秒）
            items: 本次操作涉及的条目数（读取条数/写入条数/召回条数）
        """
        self._memory_ops[(op, backend, tenant_id or "", result)].inc(1.0)
        self._memory_durations[(op, backend)].observe(duration_ms)
        if items:
            self._memory_items[(op, backend)].inc(float(items))
        with self._lock:
            # 后端健康度取最近一次操作结果：error 记 0，其余记 1
            self._memory_health[backend] = 0 if result == "error" else 1

    def record_memory_eviction(self, backend: str, count: int = 1) -> None:
        """记录一次记忆容量淘汰（差距分析 2.8：淘汰频率可观测）."""
        self._memory_evictions[backend].inc(float(count))

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

        同时写入两处：
        - 旧版兼容字段 tool_call_counts / tool_error_counts
        - 计数器 react_ai_tool_calls_total{tool,tenant,error_type,error_code}
          与直方图 react_ai_tool_call_duration_ms{tool,tenant}

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
            memory_health = dict(self._memory_health)

        prompt_renders: Dict[str, dict] = {}
        for (name, version), counter in self._prompt_renders.items():
            hist = self._prompt_durations[(name, version)]
            prompt_renders[f"prompt={name},version={version}"] = {
                "count": int(counter.value),
                "avg_ms": round(hist.avg, 2),
                "total_tokens": int(self._prompt_tokens[(name, version)].value),
            }

        memory_ops: Dict[str, dict] = {}
        for (op, backend, tenant, result), counter in self._memory_ops.items():
            hist = self._memory_durations[(op, backend)]
            label = f"op={op},backend={backend},tenant={tenant},result={result}"
            memory_ops[label] = {
                "count": int(counter.value),
                "avg_ms": round(hist.avg, 2),
            }

        # 召回命中率：按 op+backend 聚合 hit/(hit+miss)
        _per_op: Dict[Tuple[str, str], Dict[str, int]] = defaultdict(dict)
        for (op, backend, _tenant, result), counter in self._memory_ops.items():
            if result in ("hit", "miss"):
                bucket = _per_op[(op, backend)]
                bucket[result] = bucket.get(result, 0) + int(counter.value)
        memory_hit_rate: Dict[str, dict] = {}
        for (op, backend), bucket in _per_op.items():
            hits = bucket.get("hit", 0)
            misses = bucket.get("miss", 0)
            total = hits + misses
            memory_hit_rate[f"op={op},backend={backend}"] = {
                "hits": hits,
                "misses": misses,
                "hit_rate": round(hits / total, 4) if total else 0.0,
            }

        return {
            "total_requests": self.total_requests,
            "total_tokens": self.total_tokens,
            "tool_call_counts": dict(self.tool_call_counts),
            "tool_error_counts": dict(self.tool_error_counts),
            "counters": counters,
            "histograms": histograms,
            # 提示词渲染指标（§2.7）
            "prompt_renders": prompt_renders,
            # 记忆路径指标（差距分析 2.8）
            "memory_ops": memory_ops,
            "memory_hit_rate": memory_hit_rate,
            "memory_evictions": {
                backend: int(counter.value)
                for backend, counter in self._memory_evictions.items()
            },
            "memory_backend_health": memory_health,
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

        # 提示词渲染指标（§2.7）
        lines.append("# HELP react_ai_prompt_renders_total Total prompt renders by name/version")
        lines.append("# TYPE react_ai_prompt_renders_total counter")
        for (name, version), counter in self._prompt_renders.items():
            lines.append(f'react_ai_prompt_renders_total{{prompt="{name}",version="{version}"}} {int(counter.value)}')

        lines.append("# HELP react_ai_prompt_render_duration_ms Prompt render duration in milliseconds")
        lines.append("# TYPE react_ai_prompt_render_duration_ms summary")
        for (name, version), hist in self._prompt_durations.items():
            lines.append(
                f'react_ai_prompt_render_duration_ms_count{{prompt="{name}",version="{version}"}} {hist.count}'
            )
            lines.append(
                f'react_ai_prompt_render_duration_ms_sum{{prompt="{name}",version="{version}"}} {round(hist.sum, 2)}'
            )

        # 记忆路径指标（差距分析 2.8）
        lines.append("# HELP react_ai_memory_ops_total Total memory operations by op/backend/tenant/result")
        lines.append("# TYPE react_ai_memory_ops_total counter")
        for (op, backend, tenant, result), counter in self._memory_ops.items():
            lines.append(
                f'react_ai_memory_ops_total{{op="{op}",backend="{backend}",'
                f'tenant="{tenant}",result="{result}"}} {int(counter.value)}'
            )

        lines.append("# HELP react_ai_memory_op_duration_ms Memory operation duration in milliseconds")
        lines.append("# TYPE react_ai_memory_op_duration_ms summary")
        for (op, backend), hist in self._memory_durations.items():
            lines.append(
                f'react_ai_memory_op_duration_ms_count{{op="{op}",backend="{backend}"}} {hist.count}'
            )
            lines.append(
                f'react_ai_memory_op_duration_ms_sum{{op="{op}",backend="{backend}"}} {round(hist.sum, 2)}'
            )

        lines.append("# HELP react_ai_memory_items_total Total memory items processed by op/backend")
        lines.append("# TYPE react_ai_memory_items_total counter")
        for (op, backend), counter in self._memory_items.items():
            lines.append(
                f'react_ai_memory_items_total{{op="{op}",backend="{backend}"}} {int(counter.value)}'
            )

        lines.append("# HELP react_ai_memory_evictions_total Total memory evictions by backend")
        lines.append("# TYPE react_ai_memory_evictions_total counter")
        for backend, counter in self._memory_evictions.items():
            lines.append(
                f'react_ai_memory_evictions_total{{backend="{backend}"}} {int(counter.value)}'
            )

        lines.append("# HELP react_ai_memory_backend_up Memory backend health (1=last op ok, 0=last op error)")
        lines.append("# TYPE react_ai_memory_backend_up gauge")
        for backend, healthy in self._memory_health.items():
            lines.append(
                f'react_ai_memory_backend_up{{backend="{backend}"}} {healthy}'
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
            self._memory_health.clear()
        self._prompt_renders.clear()
        self._prompt_durations.clear()
        self._prompt_tokens.clear()
        self._memory_ops.clear()
        self._memory_durations.clear()
        self._memory_items.clear()
        self._memory_evictions.clear()


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
