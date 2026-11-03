"""工具沙箱隔离执行与安全拦截器.

执行顺序：鉴权 → HITL 闸门 → 熔断器 → 限流 → 结果缓存 → 舱壁信号量 → acall（含超时）。
同步工具的 execute 已在 BaseTool.acall 中通过 asyncio.to_thread 卸载，
此处只需统一经 acall 入口，保证超时/取消能正确作用于协程。

可观测性：每次 run_safe 调用都会被 tracer 包裹、metrics 记录、audit 落库，
覆盖所有退出路径（含鉴权拒绝、熔断打开、限流、超时、缓存命中等）。
"""
import asyncio
import hashlib
import json
import time
import uuid
from typing import Any, Dict, Optional

from react_ai.config.settings import settings
from react_ai.core.context import RunContext
from react_ai.observability import get_audit_store, metrics, tracer
from react_ai.observability.audit import ToolCallAuditRecord
from react_ai.tools.auth import DefaultAuthorizationService, ToolAuthorizationService
from react_ai.tools.base import BaseTool, ToolContext, ToolExecutionError, ToolResult
from react_ai.tools.resilience import (
    Bulkhead,
    CircuitBreakerRegistry,
    RateLimiter,
    ResultCache,
    get_bulkhead,
    get_circuit_breaker_registry,
    get_rate_limiter,
    get_result_cache,
)


class ToolSandbox:
    def __init__(
        self,
        default_timeout: float = 30.0,
        approval_service: Optional[Any] = None,
        authorization_service: Optional[ToolAuthorizationService] = None,
        circuit_breakers: Optional[CircuitBreakerRegistry] = None,
        rate_limiter: Optional[RateLimiter] = None,
        bulkhead: Optional[Bulkhead] = None,
        result_cache: Optional[ResultCache] = None,
        audit_store: Optional[Any] = None,
    ):
        self.default_timeout = default_timeout
        # 注入 ApprovalService 后启用 HITL 审批闸门；为 None 时保持旧行为（不拦截）
        self.approval_service = approval_service
        # 鉴权服务：默认实现（租户→工具白名单），可替换为对接 IAM 的真实实现
        self.authorization_service = authorization_service or DefaultAuthorizationService()
        self.circuit_breakers = circuit_breakers or get_circuit_breaker_registry()
        self.rate_limiter = rate_limiter or get_rate_limiter()
        self.bulkhead = bulkhead or get_bulkhead()
        self.result_cache = result_cache or get_result_cache()
        # 可观测性三组件：tracer/metrics 用模块单例，audit 可注入替换
        self.tracer = tracer
        self.metrics = metrics
        self.audit_store = audit_store or get_audit_store()

    def _build_tool_context(self, context: Optional[Any] = None, tool_call_id: Optional[str] = None) -> ToolContext:
        if isinstance(context, ToolContext):
            return context
        if isinstance(context, RunContext):
            # 幂等键优先级：业务级 metadata 显式指定 > 调用级 tool_call_id 自动生成
            idempotency_key = context.metadata.get("idempotency_key") or tool_call_id
            return ToolContext(
                caller_id=context.metadata.get("caller_id", "react-agent"),
                tenant_id=context.metadata.get("tenant_id", "default"),
                user_id=context.user_id or "anonymous",
                trace_id=context.run_id or uuid.uuid4().hex,
                idempotency_key=idempotency_key,
            )
        return ToolContext(
            caller_id="react-agent",
            tenant_id="default",
            user_id="anonymous",
            trace_id=uuid.uuid4().hex,
            idempotency_key=tool_call_id,
        )

    def _enforce_hitl_gate(
        self,
        tool: BaseTool,
        arguments: Dict[str, Any],
        context: Optional[Any],
        tool_ctx: ToolContext,
        tool_call_id: Optional[str],
    ) -> None:
        """HITL 审批闸门：需审批且未获批时抛 ApprovalRequiredError 挂起运行.

        判定口径与 get_definition().requires_approval 一致：
        显式声明 requires_approval 或副作用为 write/destructive 的工具均纳入审批。
        """
        if self.approval_service is None or not settings.security.enable_hitl:
            return
        if not tool.get_definition().requires_approval:
            return
        run_id = getattr(context, "run_id", None) or "adhoc"
        self.approval_service.evaluate_gate(
            tool_name=tool.name,
            side_effects=tool.side_effects,
            arguments=arguments,
            run_id=run_id,
            tool_call_id=tool_call_id or uuid.uuid4().hex,
            tenant_id=tool_ctx.tenant_id,
            user_id=tool_ctx.user_id,
            trace_id=tool_ctx.trace_id,
        )

    def _check_circuit(self, tool: BaseTool) -> Optional[ToolResult]:
        """熔断器检查：open 状态下直接返回可回喂的 circuit_open 错误."""
        if not settings.security.enable_circuit_breaker:
            return None
        cb = self.circuit_breakers.get(tool.name)
        if not cb.allow_request():
            return ToolResult(
                ok=False,
                error_type="external",
                error_code="circuit_open",
                error_message=f"工具 [{tool.name}] 熔断器已打开，暂时快速失败，请稍后重试",
                retryable=True,
                tool_name=tool.name,
            )
        return None

    def _check_rate_limit(self, tool: BaseTool, tool_ctx: ToolContext) -> Optional[ToolResult]:
        """三维限流检查：超限返回可回喂的 rate_limited 错误."""
        if not settings.security.enable_rate_limit:
            return None
        if not self.rate_limiter.allow(tool_ctx.tenant_id, tool_ctx.user_id, tool.name):
            return ToolResult(
                ok=False,
                error_type="external",
                error_code="rate_limited",
                error_message=f"工具 [{tool.name}] 触发限流，请稍后重试",
                retryable=True,
                tool_name=tool.name,
            )
        return None

    def _check_cache(self, tool: BaseTool, tool_ctx: ToolContext, arguments: Dict[str, Any]) -> Optional[ToolResult]:
        """结果缓存：仅 read/none 副作用工具查缓存，命中直接重放."""
        if not settings.security.enable_result_cache:
            return None
        if tool.side_effects not in ("none", "read"):
            return None
        cached = self.result_cache.get(tool.name, tool_ctx.tenant_id, arguments)
        if cached is not None:
            return ToolResult(
                ok=cached.ok,
                data=cached.data,
                tool_name=tool.name,
                trace_id=tool_ctx.trace_id,
                replayed=True,
            )
        return None

    def _record_outcome(
        self,
        tool: BaseTool,
        tool_ctx: ToolContext,
        tool_call_id: Optional[str],
        arguments: Dict[str, Any],
        duration_ms: float,
        success: bool,
        error_type: Optional[str],
        error_code: Optional[str],
        error_message: Optional[str],
        replayed: bool = False,
    ) -> None:
        """统一记录指标 + 审计 + span 属性（所有退出路径共用）."""
        # 1. 指标：tool/tenant/error_type/error_code 四维
        self.metrics.record_tool_call(
            tool_name=tool.name,
            tenant_id=tool_ctx.tenant_id,
            success=success,
            error_type=error_type or "none",
            error_code=error_code or "none",
            duration_ms=duration_ms,
        )

        # 2. 审计落库（失败不阻断主流程，PostgresAuditStore 内部已捕获异常）
        try:
            self.audit_store.record(ToolCallAuditRecord(
                trace_id=tool_ctx.trace_id,
                tool_call_id=tool_call_id,
                tool_name=tool.name,
                tenant_id=tool_ctx.tenant_id,
                user_id=tool_ctx.user_id,
                arguments=self._sanitize_arguments(arguments, tool),
                side_effects=tool.side_effects,
                success=success,
                error_type=error_type,
                error_code=error_code,
                error_message=error_message,
                duration_ms=duration_ms,
                replayed=replayed,
            ))
        except Exception:
            pass

    @staticmethod
    def _sanitize_arguments(arguments: Dict[str, Any], tool: BaseTool) -> Dict[str, Any]:
        """审计用参数摘要：避免敏感参数明文落库.

        生产环境可按 tool.name 做字段级脱敏（如 phone/id_card/password）。
        """
        SENSITIVE_KEYS = {"password", "token", "secret", "api_key", "authorization"}
        sanitized: Dict[str, Any] = {}
        for k, v in (arguments or {}).items():
            if k.lower() in SENSITIVE_KEYS:
                sanitized[k] = "***"
            else:
                sanitized[k] = v
        return sanitized

    @staticmethod
    def _arguments_digest(arguments: Dict[str, Any]) -> str:
        """span 用参数摘要：脱敏后哈希，避免明文进链路系统又能关联同参数调用."""
        try:
            payload = json.dumps(
                ToolSandbox._sanitize_arguments(arguments, None),
                sort_keys=True, ensure_ascii=False, default=str,
            )
        except Exception:
            payload = repr(arguments)
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]

    async def run_safe(
        self,
        tool: BaseTool,
        arguments: Dict[str, Any],
        context: Optional[RunContext] = None,
        tool_call_id: Optional[str] = None,
        attempt: int = 1,
    ) -> Any:
        timeout = getattr(tool, "timeout_seconds", None) or self.default_timeout
        tool_ctx = self._build_tool_context(context, tool_call_id=tool_call_id)
        start_time = time.time()

        # 可观测性：用 span 包裹整个沙箱生命周期（含所有拒绝分支）
        span_attrs = {
            "tool": tool.name,
            "tenant_id": tool_ctx.tenant_id,
            "user_id": tool_ctx.user_id,
            "trace_id": tool_ctx.trace_id,
            "tool_call_id": tool_call_id or "",
            "side_effects": tool.side_effects,
            "retry_attempt": attempt,
            "arguments_digest": self._arguments_digest(arguments),
        }

        # 退出路径结果追踪
        outcome: Dict[str, Any] = {
            "success": True,
            "error_type": None,
            "error_code": None,
            "error_message": None,
            "replayed": False,
        }

        try:
            async with self.tracer.start_span(f"tool.call.{tool.name}", span_attrs) as span:
                # 0. 取消传播：执行前检查协作式取消信号
                if isinstance(context, RunContext):
                    context.check_cancelled()

                # 1. 鉴权（RBAC/ABAC）
                self.authorization_service.authorize(tool, arguments, tool_ctx)

                # 2. HITL 审批闸门
                self._enforce_hitl_gate(tool, arguments, context, tool_ctx, tool_call_id)

                # 3. 熔断器
                circuit_err = self._check_circuit(tool)
                if circuit_err is not None:
                    raise ToolExecutionError(circuit_err)

                # 4. 限流
                rate_err = self._check_rate_limit(tool, tool_ctx)
                if rate_err is not None:
                    raise ToolExecutionError(rate_err)

                # 5. 结果缓存（仅 read/none）
                cached = self._check_cache(tool, tool_ctx, arguments)
                if cached is not None:
                    outcome["replayed"] = True
                    span.set_attribute("cache_hit", True)
                    return cached

                # 6. 舱壁信号量 + 超时执行
                tool_sem, global_sem = await self.bulkhead.acquire(tool.name)
                tool_res: Optional[ToolResult] = None
                try:
                    tool_res = await asyncio.wait_for(tool.acall(arguments, tool_ctx), timeout=timeout)
                except asyncio.TimeoutError:
                    timeout_result = ToolResult(
                        ok=False,
                        error_type="external",
                        error_code="tool_timeout",
                        error_message=f"工具 [{tool.name}] 执行超过指定时限 ({timeout}s)",
                        retryable=True,
                        tool_name=tool.name,
                    )
                    raise ToolExecutionError(timeout_result)
                finally:
                    self.bulkhead.release(tool_sem, global_sem)

                # 7. 熔断器记录结果
                if settings.security.enable_circuit_breaker:
                    self.circuit_breakers.get(tool.name).record(tool_res.ok)

                # 8. 写入结果缓存
                if (
                    settings.security.enable_result_cache
                    and tool_res.ok
                    and tool.side_effects in ("none", "read")
                ):
                    self.result_cache.set(tool.name, tool_ctx.tenant_id, arguments, tool_res)

                if not tool_res.ok:
                    raise ToolExecutionError(tool_res)

                span.set_attribute("result", "ok")
                return tool_res

        except ToolExecutionError as terr:
            outcome.update(
                success=False,
                error_type=terr.result.error_type,
                error_code=terr.result.error_code,
                error_message=terr.result.error_message,
            )
            raise
        except PermissionError as exc:
            outcome.update(
                success=False,
                error_type="auth",
                error_code="permission_denied",
                error_message=str(exc),
            )
            raise
        except Exception as exc:
            # HITL 的 ApprovalRequiredError 等非工具错误：不归入工具失败统计
            from react_ai.approval.models import ApprovalRequiredError

            if isinstance(exc, ApprovalRequiredError):
                outcome["success"] = None  # 标记为"挂起"，不计入成功/失败
                raise
            outcome.update(
                success=False,
                error_type="external",
                error_code="unexpected_error",
                error_message=str(exc),
            )
            raise
        finally:
            duration_ms = (time.time() - start_time) * 1000
            # 仅对"明确成功或失败"的调用记录指标与审计（挂起的审批不记录）
            if outcome["success"] is not None:
                self._record_outcome(
                    tool=tool,
                    tool_ctx=tool_ctx,
                    tool_call_id=tool_call_id,
                    arguments=arguments,
                    duration_ms=duration_ms,
                    success=outcome["success"],
                    error_type=outcome["error_type"],
                    error_code=outcome["error_code"],
                    error_message=outcome["error_message"],
                    replayed=outcome["replayed"],
                )
