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
        # 全局 token 计数
        self.total_tokens: int = 0
        self.total_requests: int = 0

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

    def record_request(self, tokens: int = 0) -> None:
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
            "tool_calls": calls,
            "tool_call_durations": durations,
            "prompt_renders": prompt_renders,
            # 记忆路径指标（差距分析 2.8）
            "memory_ops": memory_ops,
            "memory_hit_rate": memory_hit_rate,
            "memory_evictions": {
                backend: int(counter.value)
                for backend, counter in self._memory_evictions.items()
            },
            "memory_backend_health": dict(self._memory_health),
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

    def reset(self) -> None:
        with self._lock:
            self._calls.clear()
            self._durations.clear()
            self._prompt_renders.clear()
            self._prompt_durations.clear()
            self._prompt_tokens.clear()
            self._memory_ops.clear()
            self._memory_durations.clear()
            self._memory_items.clear()
            self._memory_evictions.clear()
            self._memory_health.clear()
            self.total_tokens = 0
            self.total_requests = 0


metrics = MetricsCollector()
