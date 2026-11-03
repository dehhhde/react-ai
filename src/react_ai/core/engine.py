"""
ReAct (Reasoning + Acting) 核心执行引擎.

驱动: Thought (思考) -> Action (行动) -> Observation (观察) 闭环迭代.
内置最大步数熔断、工具沙箱隔离、流式事件派发、异常降级以及
HITL 人工审批挂起 (checkpoint) 与审批后断点续跑 (resume).
"""
import asyncio
import json
import uuid
import time
from typing import Any, AsyncGenerator, Dict, List, Optional
from react_ai.approval.models import ApprovalRequest, ApprovalRequiredError, ApprovalStatus
from react_ai.core.base import BaseAgent
from react_ai.core.checkpoint import CheckpointStore, RunCheckpoint, get_checkpoint_store
from react_ai.core.context import RunContext
from react_ai.core.state import AgentState
from react_ai.core.lifecycle import AgentLifecycleHook
from react_ai.schemas.agent import AgentConfig, AgentResponse, StepResult
from react_ai.schemas.events import AgentEvent, AgentEventType
from react_ai.schemas.messages import ChatMessage, MessageRole, ToolCall
from react_ai.tools.base import ToolFailureBudgetExceeded, ToolResult
from react_ai.tools.registry import ToolRegistry
from react_ai.guardrails.tool_output import sanitize_observation, serialize_observation
from react_ai.llm.base import BaseLLMClient

# Observation 文本大小上限，防止大结果撑爆上下文
MAX_OBSERVATION_CHARS = 4000


def _event(event_type: AgentEventType, step: int, payload: Dict[str, Any]) -> AgentEvent:
    return AgentEvent(
        event_id=str(uuid.uuid4()),
        event_type=event_type,
        step=step,
        payload=payload,
    )


class ReActAgent(BaseAgent):
    def __init__(
        self,
        llm: BaseLLMClient,
        tool_registry: ToolRegistry,
        config: Optional[AgentConfig] = None,
        hooks: Optional[List[AgentLifecycleHook]] = None,
        approval_service: Optional[Any] = None,
        checkpoint_store: Optional[CheckpointStore] = None,
    ):
        self.llm = llm
        self.tools = tool_registry
        self.config = config or AgentConfig()
        self.hooks = hooks or []
        self.approvals = approval_service
        self.checkpoints = checkpoint_store or get_checkpoint_store()

    # ------------------------------------------------------------------
    # 同步入口
    # ------------------------------------------------------------------

    async def run(self, query: str, context: RunContext) -> AgentResponse:
        state = AgentState()
        paused_payload: Optional[Dict[str, Any]] = None
        async for event in self.stream(query, context):
            if event.event_type == AgentEventType.RUN_END:
                state.final_output = event.payload.get("output", "")
                steps_raw = event.payload.get("steps", [])
                state.step_history = [StepResult(**s) if isinstance(s, dict) else s for s in steps_raw]
                state.is_finished = True
            elif event.event_type == AgentEventType.ERROR:
                state.error = event.payload.get("error", "Unknown error")
            elif event.event_type == AgentEventType.RUN_PAUSED:
                paused_payload = event.payload

        if paused_payload is not None:
            return AgentResponse(
                session_id=context.session_id,
                run_id=context.run_id,
                final_output=paused_payload.get("message", "运行已挂起，等待人工审批"),
                steps=state.step_history,
                success=True,
                status="awaiting_approval",
                pending_approval_ids=paused_payload.get("approval_ids", []),
            )

        return AgentResponse(
            session_id=context.session_id,
            run_id=context.run_id,
            final_output=state.final_output or state.error or "执行未正常结束",
            steps=state.step_history,
            success=state.error is None,
            error_message=state.error,
        )

    # ------------------------------------------------------------------
    # 流式入口
    # ------------------------------------------------------------------

    async def stream(self, query: str, context: RunContext) -> AsyncGenerator[AgentEvent, None]:
        for hook in self.hooks:
            await hook.on_run_start(context, query)

        yield _event(
            AgentEventType.RUN_START,
            0,
            {"query": query, "session_id": context.session_id, "run_id": context.run_id},
        )

        state = AgentState()
        state.messages.append(ChatMessage(role=MessageRole.USER, content=query))

        try:
            async for event in self._loop(state, context):
                yield event
        except Exception as e:
            for hook in self.hooks:
                await hook.on_error(context, e)
            yield _event(AgentEventType.ERROR, state.current_step, {"error": str(e)})

    async def resume(self, run_id: str) -> AsyncGenerator[AgentEvent, None]:
        """审批决策后从挂起点恢复执行."""
        cp = self.checkpoints.load(run_id)
        if cp is None:
            raise KeyError(f"未找到运行 [{run_id}] 的挂起检查点")
        if self.approvals is None:
            raise RuntimeError("Agent 未配置审批服务，无法恢复挂起运行")

        context = RunContext(
            session_id=cp.session_id,
            run_id=cp.run_id,
            user_id=cp.user_id,
            metadata=dict(cp.metadata),
        )
        state = AgentState(
            current_step=cp.current_step,
            messages=list(cp.messages),
            step_history=list(cp.step_history),
        )

        yield _event(
            AgentEventType.RUN_RESUMED,
            state.current_step,
            {"run_id": run_id, "pending_tool_calls": len(cp.pending_tool_calls)},
        )

        pending = cp.pending_tool_calls
        idx = 0
        while idx < len(pending):
            item = pending[idx]
            tc = ToolCall(**item["tool_call"])
            approval_id = item.get("approval_id")
            req = self.approvals.get_request(approval_id) if approval_id else None

            if req is not None and req.status == ApprovalStatus.PENDING:
                # 审批仍未决策，保持挂起
                context.is_paused = True
                context.pending_approval_ids = [req.id]
                yield _event(AgentEventType.TOOL_APPROVAL_REQUIRED, state.current_step, self._approval_payload(req))
                yield _event(
                    AgentEventType.RUN_PAUSED,
                    state.current_step,
                    {
                        "run_id": run_id,
                        "approval_ids": [req.id],
                        "message": f"工具 [{req.tool_name}] 审批仍未决策，运行保持挂起",
                    },
                )
                return

            if req is not None and req.status in (
                ApprovalStatus.REJECTED,
                ApprovalStatus.EXPIRED,
                ApprovalStatus.CONSUMED,
            ):
                yield _event(
                    AgentEventType.TOOL_APPROVAL_DECISION,
                    state.current_step,
                    {
                        "approval_id": req.id,
                        "tool": tc.name,
                        "decision": req.status.value,
                        "decided_by": req.decided_by,
                        "comment": req.decision_comment,
                    },
                )
                self._fill_rejected_observations(state, pending[idx:], req)
                break

            # 已批准（闸门消费放行）或无需审批 → 执行剩余调用；可能再次触发审批挂起
            remaining = [ToolCall(**p["tool_call"]) for p in pending[idx:]]
            async for event in self._run_tool_calls(state, context, remaining):
                yield event
            if context.is_paused:
                return
            break

        self.checkpoints.delete(run_id)

        try:
            async for event in self._loop(state, context):
                yield event
        except Exception as e:
            for hook in self.hooks:
                await hook.on_error(context, e)
            yield _event(AgentEventType.ERROR, state.current_step, {"error": str(e)})

    # ------------------------------------------------------------------
    # 内部循环
    # ------------------------------------------------------------------

    async def _loop(self, state: AgentState, context: RunContext) -> AsyncGenerator[AgentEvent, None]:
        while state.current_step < self.config.max_steps and not state.is_finished:
            context.check_cancelled()
            step_idx = state.current_step + 1

            for hook in self.hooks:
                await hook.on_step_start(context, step_idx)

            # 1. 思考阶段 (Thought)
            yield _event(AgentEventType.THOUGHT_START, step_idx, {"step": step_idx})

            # Token 预算检查：超预算优雅收尾，避免无限消耗
            if context.used_tokens >= context.budget_tokens:
                final_ans = (
                    f"已达到本轮 Token 预算上限 ({context.budget_tokens})，"
                    "无法继续调用模型，请精简问题后重试。"
                )
                state.is_finished = True
                state.add_step(StepResult(
                    step_number=step_idx,
                    thought="Token 预算耗尽，终止执行。",
                    is_final=True,
                    final_answer=final_ans,
                ))
                yield _event(
                    AgentEventType.RUN_END,
                    step_idx,
                    {"output": final_ans, "steps": [s.model_dump() for s in state.step_history]},
                )
                for hook in self.hooks:
                    await hook.on_run_end(context, final_ans)
                return

            available_tools = self.tools.list_definitions()
            llm_response = await self.llm.generate(
                messages=state.messages,
                tools=available_tools,
                temperature=self.config.temperature,
            )

            # 累计本次 LLM 调用的 token 消耗，用于预算管控
            context.used_tokens += getattr(llm_response.usage, "total_tokens", 0) or 0

            # 2. 判断是否触发行动 (Action) 或给出最终答复
            if not llm_response.tool_calls:
                final_ans = llm_response.content or "无回答"
                state.is_finished = True
                state.add_step(StepResult(
                    step_number=step_idx,
                    thought="已获得全部必要信息，形成最终答复。",
                    is_final=True,
                    final_answer=final_ans,
                ))

                yield _event(
                    AgentEventType.RUN_END,
                    step_idx,
                    {"output": final_ans, "steps": [s.model_dump() for s in state.step_history]},
                )
                for hook in self.hooks:
                    await hook.on_run_end(context, final_ans)
                return

            # 单条 assistant 消息携带本轮全部 tool_calls（协议要求每个调用都有对应 tool 消息）
            state.messages.append(ChatMessage(
                role=MessageRole.ASSISTANT,
                content=llm_response.content,
                tool_calls=llm_response.tool_calls,
            ))

            async for event in self._run_tool_calls(state, context, llm_response.tool_calls):
                yield event
            if context.is_paused:
                return

        if not state.is_finished:
            msg = f"达到最大步数上限 ({self.config.max_steps}) 熔断退出。"
            yield _event(AgentEventType.ERROR, state.current_step, {"error": msg})

    async def _run_tool_calls(
        self,
        state: AgentState,
        context: RunContext,
        tool_calls: List[ToolCall],
    ) -> AsyncGenerator[AgentEvent, None]:
        step_idx = state.current_step + 1

        # 先按顺序派发所有 TOOL_START 事件，再并行执行，保证事件流确定性
        for tool_call in tool_calls:
            yield _event(
                AgentEventType.TOOL_START,
                step_idx,
                {"tool": tool_call.name, "args": tool_call.arguments, "tool_call_id": tool_call.id},
            )

        # 并行执行所有工具调用；每个调用内部自带 retryable 重试循环
        outcomes = await asyncio.gather(
            *[self._execute_single_tool_call(context, tool_call, step_idx) for tool_call in tool_calls],
            return_exceptions=True,
        )

        # 按 tool_calls 顺序处理结果：提交已完成的观测，遇审批则挂起
        for idx, (tool_call, outcome) in enumerate(zip(tool_calls, outcomes)):
            # gather 层异常（取消等）直接抛出
            if isinstance(outcome, BaseException) and not isinstance(outcome, Exception):
                raise outcome
            if isinstance(outcome, Exception):
                yield _event(
                    AgentEventType.TOOL_ERROR,
                    step_idx,
                    {"tool": tool_call.name, "error": str(outcome)},
                )
                raise outcome

            retry_events, payload = outcome

            # 派发该调用在重试阶段产生的事件
            for ev in retry_events:
                yield ev

            # 审批挂起：保存从当前调用开始的剩余 calls 作为 checkpoint
            if isinstance(payload, ApprovalRequiredError):
                self._save_checkpoint(context, state, tool_calls[idx:], first_approval_id=payload.request.id)
                context.is_paused = True
                context.pending_approval_ids = [payload.request.id]
                yield _event(AgentEventType.TOOL_APPROVAL_REQUIRED, step_idx, self._approval_payload(payload.request))
                yield _event(
                    AgentEventType.RUN_PAUSED,
                    step_idx,
                    {
                        "run_id": context.run_id,
                        "approval_ids": [payload.request.id],
                        "message": f"工具 [{payload.request.tool_name}] 已触发人工审批闸门，运行挂起等待决策",
                    },
                )
                return

            tool_result, duration_ms = payload

            if tool_result is None:
                raise RuntimeError(f"工具 [{tool_call.name}] 未返回执行结果")

            if tool_result.ok:
                yield _event(
                    AgentEventType.TOOL_END,
                    step_idx,
                    {"tool": tool_call.name, "result": tool_result.data, "duration_ms": duration_ms},
                )
                obs_str = self._serialize_observation(tool_result.data)
                obs_str = self._apply_output_guardrails(obs_str)
                state.consecutive_tool_failures = 0
            else:
                # 失败结果作为结构化错误 Observation 回喂模型，让其自我纠错而非中断 run
                obs_str = self._format_error_observation(tool_call.name, tool_result)
                yield _event(
                    AgentEventType.TOOL_ERROR,
                    step_idx,
                    {
                        "tool": tool_call.name,
                        "error": tool_result.error_message,
                        "error_type": tool_result.error_type,
                        "error_code": tool_result.error_code,
                        "retryable": tool_result.retryable,
                        "fed_back": True,
                        "duration_ms": duration_ms,
                    },
                )
                state.consecutive_tool_failures += 1

            yield _event(AgentEventType.OBSERVATION, step_idx, {"observation": obs_str})

            state.messages.append(ChatMessage(
                role=MessageRole.TOOL,
                tool_call_id=tool_call.id,
                name=tool_call.name,
                content=obs_str,
            ))
            state.add_step(StepResult(
                step_number=step_idx,
                action_name=tool_call.name,
                action_input=tool_call.arguments,
                observation=obs_str,
            ))

            # 连续失败熔断：超过预算后终止 run，避免在错误路径上无限消耗
            if state.consecutive_tool_failures >= self.config.max_consecutive_tool_failures:
                msg = (
                    f"连续 {state.consecutive_tool_failures} 次工具调用失败，触发熔断退出 "
                    f"(阈值 {self.config.max_consecutive_tool_failures})，请人工介入。"
                )
                raise ToolFailureBudgetExceeded(msg)

    async def _execute_single_tool_call(
        self,
        context: RunContext,
        tool_call: ToolCall,
        step_idx: int,
    ) -> tuple:
        """执行单个工具调用（含 retryable 重试），返回 (retry_events, payload)。

        payload 为 ApprovalRequiredError（挂起）或 (ToolResult, duration_ms)。
        """
        retry_events: List[AgentEvent] = []
        start_time = time.time()
        tool_result: Optional[ToolResult] = None

        for attempt in range(self.config.tool_retry_attempts):
            try:
                # 规范入口 call()：失败返回 ok=False 的 ToolResult 而非抛异常，
                # 保留 error_type/retryable/replayed 供错误回环与重试判定
                tool_result = await self.tools.call(
                    name=tool_call.name,
                    arguments=tool_call.arguments,
                    context=context,
                    tool_call_id=tool_call.id,
                    attempt=attempt + 1,
                )
            except ApprovalRequiredError as ar:
                return retry_events, ar
            except Exception as err:
                # 非工具业务错误（如工具未注册、编程错误）：向上抛出由引擎处理
                return retry_events, err

            # 仅 retryable 错误（通常是 external 瞬时故障）自动重试
            if (
                not tool_result.ok
                and tool_result.retryable
                and attempt < self.config.tool_retry_attempts - 1
            ):
                backoff = 0.25 * (2 ** attempt)
                retry_events.append(_event(
                    AgentEventType.TOOL_ERROR,
                    step_idx,
                    {
                        "tool": tool_call.name,
                        "error": tool_result.error_message,
                        "error_type": tool_result.error_type,
                        "retry": True,
                        "attempt": attempt + 1,
                    },
                ))
                await asyncio.sleep(backoff)
                continue
            break

        duration_ms = (time.time() - start_time) * 1000
        return retry_events, (tool_result, duration_ms)

    @staticmethod
    def _format_error_observation(tool_name: str, result: ToolResult) -> str:
        """按 error_type 差异化生成给模型的错误提示，引导其换参数/换工具/转人工."""
        type_labels = {
            "schema": "参数错误 (schema)",
            "business": "业务规则拒绝 (business)",
            "auth": "权限不足 (auth)",
            "external": "外部服务异常 (external)",
        }
        hints = {
            "schema": "请检查并修正参数后重试。",
            "business": "该操作被业务规则拒绝，请调整方案或换用其他工具，不要直接重试相同操作。",
            "auth": "权限不足，建议转人工处理或确认调用身份。",
            "external": "外部依赖瞬时异常，可稍后重试；多次失败请换用其他工具。",
        }
        label = type_labels.get(result.error_type, "未知错误")
        hint = hints.get(result.error_type, "请检查后重试。")
        retry_tag = "（该错误可重试）" if result.retryable else ""
        return (
            f"[工具调用失败]\n"
            f"工具: {tool_name}\n"
            f"错误类型: {label}\n"
            f"错误码: {result.error_code}\n"
            f"错误信息: {result.error_message}\n"
            f"提示: {hint}{retry_tag}"
        )

    @staticmethod
    def _serialize_observation(data: Any) -> str:
        """将工具结果序列化为 JSON 字符串并做大小截断，避免 Python repr 与大结果撑爆上下文."""
        try:
            text = json.dumps(data, ensure_ascii=False, default=str)
        except (TypeError, ValueError):
            text = str(data)
        if len(text) > MAX_OBSERVATION_CHARS:
            text = text[:MAX_OBSERVATION_CHARS] + "\n...[Observation 超过大小限制已截断]"
        return text

    @staticmethod
    def _apply_output_guardrails(text: str) -> str:
        """对工具输出 Observation 应用 PII 脱敏与间接提示注入扫描.

        - PII 脱敏：手机号/身份证号掩码，防止敏感信息进入下一轮 prompt；
        - 注入扫描：若检测到注入特征，用安全兜底文本替代原始内容，禁止其进入上下文。
        护栏异常不阻断主流程，降级为原始文本。
        通过 settings.security.enable_guardrails 控制总开关。
        """
        from react_ai.config.settings import settings
        if not settings.security.enable_guardrails:
            return text
        try:
            return sanitize_observation(text)
        except Exception:
            return text

    def _fill_rejected_observations(
        self,
        state: AgentState,
        pending_items: List[Dict[str, Any]],
        req: ApprovalRequest,
    ) -> None:
        """审批拒绝/超时：为所有未执行的 tool_calls 补齐拒绝观测，保证消息协议完整."""
        for i, item in enumerate(pending_items):
            tc = ToolCall(**item["tool_call"])
            if i == 0:
                obs = (
                    f"人工审批未通过（{req.status.value}）："
                    f"{req.decision_comment or '操作被拒绝'}。请勿重试该操作。"
                )
            else:
                obs = "未执行：前序操作被人工审批拒绝，本次调用取消。"
            state.messages.append(ChatMessage(
                role=MessageRole.TOOL,
                tool_call_id=tc.id,
                name=tc.name,
                content=obs,
            ))
            state.add_step(StepResult(
                step_number=state.current_step + 1,
                action_name=tc.name,
                action_input=tc.arguments,
                observation=obs,
            ))

    def _save_checkpoint(
        self,
        context: RunContext,
        state: AgentState,
        remaining_tool_calls: List[ToolCall],
        first_approval_id: Optional[str] = None,
    ) -> None:
        pending = [
            {
                "tool_call": tc.model_dump(),
                "approval_id": first_approval_id if i == 0 else None,
            }
            for i, tc in enumerate(remaining_tool_calls)
        ]
        self.checkpoints.save(RunCheckpoint(
            run_id=context.run_id,
            session_id=context.session_id,
            user_id=context.user_id,
            metadata=dict(context.metadata),
            current_step=state.current_step,
            messages=list(state.messages),
            step_history=list(state.step_history),
            pending_tool_calls=pending,
        ))

    @staticmethod
    def _approval_payload(req: ApprovalRequest) -> Dict[str, Any]:
        return {
            "approval_id": req.id,
            "run_id": req.run_id,
            "tool": req.tool_name,
            "arguments": req.arguments,
            "side_effects": req.side_effects,
            "reason": req.reason,
            "status": req.status.value,
            "expires_at": req.expires_at,
            "resume_endpoint": f"/api/v1/agent/runs/{req.run_id}/resume",
        }
