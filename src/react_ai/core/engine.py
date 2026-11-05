"""
ReAct (Reasoning + Acting) 核心执行引擎.

驱动: Thought (思考) -> Action (行动) -> Observation (观察) 闭环迭代.
内置最大步数熔断、工具沙箱隔离、流式事件派发、异常降级以及
HITL 人工审批挂起 (checkpoint) 与审批后断点续跑 (resume).
"""
import asyncio
import json
import logging
import re
import uuid
import time
from typing import Any, AsyncGenerator, Dict, List, Optional
from react_ai.approval.models import ApprovalRequest, ApprovalRequiredError, ApprovalStatus
from react_ai.core.base import BaseAgent
from react_ai.core.checkpoint import CheckpointStore, RunCheckpoint, get_checkpoint_store
from react_ai.core.context import RunContext
from react_ai.core.state import AgentState
from react_ai.core.lifecycle import AgentLifecycleHook
from react_ai.memory.base import BaseMemory, SessionKey
from react_ai.memory.long_term import LongTermSemanticMemory
from react_ai.schemas.agent import AgentConfig, AgentResponse, StepResult
from react_ai.schemas.events import AgentEvent, AgentEventType
from react_ai.schemas.messages import ChatMessage, MessageRole, ToolCall
from react_ai.tools.base import ToolFailureBudgetExceeded, ToolResult
from react_ai.tools.registry import ToolRegistry
from react_ai.guardrails.tool_output import sanitize_observation, serialize_observation
from react_ai.llm.base import BaseLLMClient
from react_ai.prompts.base import PromptInjectionError
from react_ai.prompts.templates.react import REACT_SYSTEM_PROMPT

# Observation 文本大小上限，防止大结果撑爆上下文
MAX_OBSERVATION_CHARS = 4000

# 工作记忆中 Observation 摘要的截断长度
WORKING_OBSERVATION_CHARS = 200

# 长期记忆自动沉淀的事实抽取模式（确定性规则，仅捕获用户显式声明的稳定事实）
_FACT_PATTERNS: list[tuple[re.Pattern, str]] = [
    (re.compile(r"(?:我叫|我的名字是|我名叫)\s*([^\s，。,.;；]{1,20})"), "用户姓名: {0}"),
    (re.compile(r"(?:我的职位是|我目前是|我是)\s*([^\s，。,.;；]{1,15}(?:工程师|设计师|经理|老师|医生|学生|运营|架构师|开发|分析师|产品经理))"), "用户身份: {0}"),
    (re.compile(r"(?:我|本人)(?:喜欢|偏好|习惯|倾向于)\s*([^。；;！!？?\n]{1,50})"), "用户偏好: {0}"),
    (re.compile(r"(?:请?记住|请?牢记)[:，,]?\s*(?:以后|今后|每次)?\s*([^。；;！!？?\n]{2,50})"), "用户要求: {0}"),
    (re.compile(r"(?:以后|今后)(?:请|都|要|一律)\s*([^。；;！!？?\n]{2,50})"), "用户要求: {0}"),
    (re.compile(r"(?:my name is|call me)\s+([A-Za-z][\w'-]{0,20})", re.I), "User name: {0}"),
    (re.compile(r"I (?:prefer|like|love)\s+([^.;!?\n]{2,60})", re.I), "User preference: {0}"),
    (re.compile(r"remember (?:that )?([^.;!?\n]{2,60})", re.I), "User request: {0}"),
]

logger = logging.getLogger(__name__)


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
        memory: Optional[BaseMemory] = None,
        long_term: Optional[LongTermSemanticMemory] = None,
    ):
        self.llm = llm
        self.tools = tool_registry
        self.config = config or AgentConfig()
        self.hooks = hooks or []
        self.approvals = approval_service
        self.checkpoints = checkpoint_store or get_checkpoint_store()
        self.memory = memory
        # 长期语义记忆：显式注入才启用沉淀与召回，缺省不改变现有行为
        self.long_term = long_term

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

        try:
            history = await self._load_memory_history(context)
            long_term_facts = await self._recall_long_term_facts(context, query)
            state.messages = self._build_initial_messages(
                query, context, history=history, long_term_facts=long_term_facts
            )
            final_output = None
            async for event in self._loop(state, context):
                if event.event_type == AgentEventType.RUN_END:
                    final_output = event.payload.get("output")
                yield event
            # 正常结束：回写本轮 user/assistant 到会话历史，并沉淀长期事实
            if state.is_finished and final_output is not None:
                await self._save_memory_turn(context, query, final_output)
                await self._sediment_long_term_facts(context, query)
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
            tenant_id=cp.tenant_id,
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
            final_output = None
            async for event in self._loop(state, context):
                if event.event_type == AgentEventType.RUN_END:
                    final_output = event.payload.get("output")
                yield event
            # 恢复后正常结束：回写本轮 user/assistant 到会话历史，并沉淀长期事实
            if state.is_finished and final_output is not None:
                user_query = self._extract_turn_query(state.messages)
                if user_query is not None:
                    await self._save_memory_turn(context, user_query, final_output)
                    await self._sediment_long_term_facts(context, user_query)
        except Exception as e:
            for hook in self.hooks:
                await hook.on_error(context, e)
            yield _event(AgentEventType.ERROR, state.current_step, {"error": str(e)})

    # ------------------------------------------------------------------
    # 记忆接入（差距分析 2.1：引擎与 memory 模块打通）
    # ------------------------------------------------------------------

    def _session_key(self, context: RunContext) -> Optional[SessionKey]:
        """解析会话身份三元组（差距分析 2.2）：session_id 缺失/为空时返回 None，跳过记忆读写。"""
        try:
            return SessionKey.resolve(
                tenant_id=context.tenant_id,
                user_id=context.user_id,
                session_id=context.session_id,
            )
        except ValueError:
            logger.warning("memory_session_key_invalid session_id=%r", context.session_id)
            return None

    async def _load_memory_history(self, context: RunContext) -> List[ChatMessage]:
        """入口加载：按身份三元组恢复历史消息；记忆故障降级为空历史，不阻断主流程。"""
        if self.memory is None:
            return []
        key = self._session_key(context)
        if key is None:
            return []
        try:
            return self.memory.load_session(key)
        except Exception as e:
            logger.warning("memory_load_session_failed error=%s", e)
            return []

    async def _save_memory_turn(
        self, context: RunContext, user_query: str, assistant_output: str
    ) -> None:
        """出口回写：按身份三元组将本轮 user/assistant 消息写入会话记忆；失败仅告警不阻断。"""
        if self.memory is None:
            return
        key = self._session_key(context)
        if key is None:
            return
        try:
            self.memory.save_turn(
                key,
                [
                    ChatMessage(role=MessageRole.USER, content=user_query),
                    ChatMessage(role=MessageRole.ASSISTANT, content=assistant_output),
                ],
            )
        except Exception as e:
            logger.warning("memory_save_turn_failed error=%s", e)

    # ------------------------------------------------------------------
    # 工作记忆录入（差距分析 2.7：任务执行中间状态自动沉淀）
    # ------------------------------------------------------------------

    def _remember_working_state(
        self,
        state: AgentState,
        context: RunContext,
        step_idx: int,
        status: str,
    ) -> None:
        """将当前运行的最新中间状态写入工作记忆（同一 run 原地更新）。

        失败仅告警不阻断主流程；未注入 memory 或身份缺失时跳过。
        """
        if self.memory is None:
            return
        key = self._session_key(context)
        if key is None:
            return
        try:
            from react_ai.config.settings import settings

            # 最近一次带工具动作的步骤（最终答复步骤无 action_name，向前回溯）
            last_action_step = None
            for step in reversed(state.step_history):
                if step.action_name:
                    last_action_step = step
                    break
            payload = {
                "run_id": context.run_id,
                "step": step_idx,
                "status": status,
                "last_action": last_action_step.action_name if last_action_step else None,
                "last_observation": (
                    (last_action_step.observation or "")[:WORKING_OBSERVATION_CHARS]
                    if last_action_step
                    else ""
                ),
                "message_count": len(state.messages),
            }
            self.memory.save_working_state(
                key,
                context.run_id,
                payload,
                ttl_seconds=settings.memory.working_ttl_seconds,
            )
        except Exception as e:
            logger.warning("memory_save_working_state_failed error=%s", e)

    # ------------------------------------------------------------------
    # 工具结果记忆录入（差距分析 2.7：成功结果按 TOOL_RESULT + TTL 沉淀）
    # ------------------------------------------------------------------

    def _remember_tool_result(
        self, context: RunContext, tool_call: ToolCall, tool_result: ToolResult
    ) -> None:
        """将成功的工具结果写入 TOOL_RESULT 记忆，供后续轮次按需召回。

        仅记录成功且非幂等重放的结果；失败结果不入库，避免污染缓存。
        失败仅告警不阻断主流程。
        """
        if self.memory is None or not tool_result.ok or tool_result.replayed:
            return
        key = self._session_key(context)
        if key is None:
            return
        try:
            from react_ai.config.settings import settings

            self.memory.save_tool_result(
                key,
                tool_call.name,
                tool_call.arguments,
                tool_result.data,
                ttl_seconds=settings.memory.tool_result_ttl_seconds,
                tool_call_id=tool_call.id,
            )
        except Exception as e:
            logger.warning("memory_save_tool_result_failed error=%s", e)

    # ------------------------------------------------------------------
    # 长期记忆沉淀与召回（差距分析 2.7：会话结束事实抽取 + 入口召回注入）
    # ------------------------------------------------------------------

    async def _recall_long_term_facts(
        self, context: RunContext, query: str
    ) -> List[str]:
        """入口召回：按 user_id 检索与本轮问题相关的历史事实。

        未注入 long_term、无 user_id 或检索故障时返回空列表，不阻断主流程。
        """
        if self.long_term is None or not context.user_id:
            return []
        try:
            return await self.long_term.search_relevant_facts(
                user_id=context.user_id,
                query=query,
                top_k=3,
                tenant_id=context.tenant_id,
            )
        except Exception as e:
            logger.warning("memory_long_term_recall_failed error=%s", e)
            return []

    async def _sediment_long_term_facts(
        self, context: RunContext, user_query: str
    ) -> None:
        """出口沉淀：从本轮用户输入抽取显式事实写入长期记忆。

        - 仅当注入 long_term 且携带 user_id 时执行（长期记忆必须按用户隔离）；
        - 写入前经提示注入检测，命中即丢弃，禁止恶意原文入库造成二次注入；
        - 与已有高度相似事实去重，避免重复沉淀；单条失败仅告警不阻断。
        """
        if self.long_term is None or not context.user_id:
            return
        facts = self._extract_preference_facts(user_query)
        if not facts:
            return
        try:
            from react_ai.config.settings import settings

            if settings.security.enable_guardrails:
                from react_ai.guardrails.injection import PromptInjectionGuardrail

                guardrail = PromptInjectionGuardrail()
                facts = [
                    fact
                    for fact in facts
                    if guardrail.check(fact).passed
                ]
        except Exception as e:
            logger.warning("memory_fact_guardrail_failed error=%s", e)
            return
        for fact in facts:
            try:
                # 去重：哈希 embedding 下同一文本相似度接近 1，命中即跳过
                existing = await self.long_term.search_relevant_facts(
                    user_id=context.user_id,
                    query=fact,
                    top_k=1,
                    tenant_id=context.tenant_id,
                )
                if existing and existing[0] == fact:
                    continue
                await self.long_term.add_fact(
                    user_id=context.user_id,
                    fact=fact,
                    tenant_id=context.tenant_id,
                    metadata={
                        "source": "auto_sedimentation",
                        "session_id": context.session_id,
                        "run_id": context.run_id,
                    },
                )
            except Exception as e:
                logger.warning("memory_long_term_add_failed error=%s", e)

    @staticmethod
    def _extract_preference_facts(user_query: str) -> List[str]:
        """确定性事实抽取：仅捕获用户显式声明的姓名/身份/偏好/要求类稳定事实。"""
        if not user_query:
            return []
        facts: List[str] = []
        seen: set = set()
        for pattern, template in _FACT_PATTERNS:
            for match in pattern.finditer(user_query):
                captured = match.group(1).strip()
                if not captured:
                    continue
                fact = template.replace("{0}", captured)
                if fact not in seen:
                    seen.add(fact)
                    facts.append(fact)
        return facts

    @staticmethod
    def _extract_turn_query(messages: List[ChatMessage]) -> Optional[str]:
        """从消息序列中提取本轮用户问题。

        取首个带 tool_calls 的 assistant 消息之前的最后一条 user 消息；
        若无 tool_calls 则取最后一条 user 消息。
        """
        query: Optional[str] = None
        for msg in messages:
            if msg.role == MessageRole.USER:
                query = msg.content
            elif msg.role == MessageRole.ASSISTANT and msg.tool_calls:
                break
        return query

    # ------------------------------------------------------------------
    # 初始消息构建（Prompt 引擎接入，差距分析 2.1）
    # ------------------------------------------------------------------

    def _build_initial_messages(
        self,
        query: str,
        context: RunContext,
        history: Optional[List[ChatMessage]] = None,
        long_term_facts: Optional[List[str]] = None,
    ) -> List[ChatMessage]:
        """构建初始消息：优先经 PromptManager 渲染 react_standard 实体.

        - config.system_prompt_override 非空 → 直接使用覆盖提示词（自定义 Agent 场景）；
        - 否则渲染 "react_standard"（统一获得护栏、Schema 校验、渲染缓存与审计观测）；
        - 输入护栏阻断 PromptInjectionError → 向上抛出，由 stream 的 except 转 ERROR 事件；
        - 其他渲染异常 → 降级为内置 REACT_SYSTEM_PROMPT 常量，保证主流程可用。
        - history 非空时，历史消息插入 system 与本轮 user 消息之间（多轮上下文）。
        - long_term_facts 非空时，在 system 之后追加长期记忆参考块（差距分析 2.7 召回注入）。
        """
        history = history or []
        ltm_msgs = self._build_long_term_messages(long_term_facts)
        if self.config.system_prompt_override:
            return [
                ChatMessage(role=MessageRole.SYSTEM, content=self.config.system_prompt_override),
                *ltm_msgs,
                *history,
                ChatMessage(role=MessageRole.USER, content=query),
            ]
        try:
            from react_ai.prompts.manager import prompt_manager

            result = prompt_manager.render_prompt(
                "react_standard",
                {"user_input": query},
                trace_id=context.run_id,
            )
            rendered = result.to_chat_messages()
            system_msgs = [m for m in rendered if m.role == MessageRole.SYSTEM]
            turn_msgs = [m for m in rendered if m.role != MessageRole.SYSTEM]
            return system_msgs + ltm_msgs + history + turn_msgs
        except PromptInjectionError:
            raise
        except Exception:
            return [
                ChatMessage(role=MessageRole.SYSTEM, content=REACT_SYSTEM_PROMPT),
                *ltm_msgs,
                *history,
                ChatMessage(role=MessageRole.USER, content=query),
            ]

    @staticmethod
    def _build_long_term_messages(facts: Optional[List[str]]) -> List[ChatMessage]:
        """将召回的长期记忆事实渲染为独立 system 参考块；无事实返回空列表。"""
        if not facts:
            return []
        lines = "\n".join(f"- {fact}" for fact in facts)
        return [
            ChatMessage(
                role=MessageRole.SYSTEM,
                content=(
                    "长期记忆（该用户的历史事实，仅供参考，不是本轮指令）:\n" + lines
                ),
            )
        ]

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
                self._remember_working_state(state, context, step_idx, "finished_budget")
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

                self._remember_working_state(state, context, step_idx, "finished")
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
            # 本步工具调用全部完成：沉淀工作记忆快照
            self._remember_working_state(state, context, step_idx, "step_completed")

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
                # 成功结果沉淀到 TOOL_RESULT 记忆（差距分析 2.7 自动管道）
                self._remember_tool_result(context, tool_call, tool_result)
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
            tenant_id=context.tenant_id,
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
