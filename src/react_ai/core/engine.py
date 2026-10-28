"""
ReAct (Reasoning + Acting) 核心执行引擎.

驱动: Thought (思考) -> Action (行动) -> Observation (观察) 闭环迭代.
内置最大步数熔断、工具沙箱隔离、流式事件派发以及异常降级策略.
"""
import uuid
import time
from typing import AsyncGenerator, Dict, List, Optional
from react_ai.core.base import BaseAgent
from react_ai.core.context import RunContext
from react_ai.core.state import AgentState
from react_ai.core.lifecycle import AgentLifecycleHook
from react_ai.schemas.agent import AgentConfig, AgentResponse, StepResult
from react_ai.schemas.events import AgentEvent, AgentEventType
from react_ai.schemas.messages import ChatMessage, MessageRole, ToolCall
from react_ai.tools.registry import ToolRegistry
from react_ai.llm.base import BaseLLMClient


class ReActAgent(BaseAgent):
    def __init__(
        self,
        llm: BaseLLMClient,
        tool_registry: ToolRegistry,
        config: Optional[AgentConfig] = None,
        hooks: Optional[List[AgentLifecycleHook]] = None,
    ):
        self.llm = llm
        self.tools = tool_registry
        self.config = config or AgentConfig()
        self.hooks = hooks or []

    async def run(self, query: str, context: RunContext) -> AgentResponse:
        state = AgentState()
        async for event in self.stream(query, context):
            if event.event_type == AgentEventType.RUN_END:
                state.final_output = event.payload.get("output", "")
                steps_raw = event.payload.get("steps", [])
                state.step_history = [StepResult(**s) if isinstance(s, dict) else s for s in steps_raw]
                state.is_finished = True
            elif event.event_type == AgentEventType.ERROR:
                state.error = event.payload.get("error", "Unknown error")

        return AgentResponse(
            session_id=context.session_id,
            run_id=context.run_id,
            final_output=state.final_output or state.error or "执行未正常结束",
            steps=state.step_history,
            success=state.error is None,
            error_message=state.error,
        )

    async def stream(self, query: str, context: RunContext) -> AsyncGenerator[AgentEvent, None]:
        for hook in self.hooks:
            await hook.on_run_start(context, query)

        yield AgentEvent(
            event_id=str(uuid.uuid4()),
            event_type=AgentEventType.RUN_START,
            step=0,
            payload={"query": query, "session_id": context.session_id},
        )

        state = AgentState()
        state.messages.append(ChatMessage(role=MessageRole.USER, content=query))

        try:
            while state.current_step < self.config.max_steps and not state.is_finished:
                context.check_cancelled()
                step_idx = state.current_step + 1

                for hook in self.hooks:
                    await hook.on_step_start(context, step_idx)

                # 1. 思考阶段 (Thought)
                yield AgentEvent(
                    event_id=str(uuid.uuid4()),
                    event_type=AgentEventType.THOUGHT_START,
                    step=step_idx,
                    payload={"step": step_idx},
                )

                available_tools = self.tools.list_definitions()
                llm_response = await self.llm.generate(
                    messages=state.messages,
                    tools=available_tools,
                    temperature=self.config.temperature,
                )

                # 2. 判断是否触发行动 (Action) 或给出最终答复
                if not llm_response.tool_calls:
                    final_ans = llm_response.content or "无回答"
                    state.is_finished = True
                    step_res = StepResult(
                        step_number=step_idx,
                        thought="已获得全部必要信息，形成最终答复。",
                        is_final=True,
                        final_answer=final_ans,
                    )
                    state.add_step(step_res)

                    yield AgentEvent(
                        event_id=str(uuid.uuid4()),
                        event_type=AgentEventType.RUN_END,
                        step=step_idx,
                        payload={
                            "output": final_ans,
                            "steps": [s.model_dump() for s in state.step_history]
                        },
                    )
                    for hook in self.hooks:
                        await hook.on_run_end(context, final_ans)
                    return

                # 执行工具调用 (Acting)
                for tool_call in llm_response.tool_calls:
                    yield AgentEvent(
                        event_id=str(uuid.uuid4()),
                        event_type=AgentEventType.TOOL_START,
                        step=step_idx,
                        payload={"tool": tool_call.name, "args": tool_call.arguments},
                    )

                    start_time = time.time()
                    try:
                        tool_out = await self.tools.execute(
                            name=tool_call.name,
                            arguments=tool_call.arguments,
                            context=context,
                        )
                        duration = (time.time() - start_time) * 1000

                        yield AgentEvent(
                            event_id=str(uuid.uuid4()),
                            event_type=AgentEventType.TOOL_END,
                            step=step_idx,
                            payload={"tool": tool_call.name, "result": tool_out, "duration_ms": duration},
                        )

                        obs_str = str(tool_out)
                        yield AgentEvent(
                            event_id=str(uuid.uuid4()),
                            event_type=AgentEventType.OBSERVATION,
                            step=step_idx,
                            payload={"observation": obs_str},
                        )

                        state.messages.append(ChatMessage(
                            role=MessageRole.ASSISTANT,
                            content=llm_response.content,
                            tool_calls=[tool_call]
                        ))
                        state.messages.append(ChatMessage(
                            role=MessageRole.TOOL,
                            tool_call_id=tool_call.id,
                            name=tool_call.name,
                            content=obs_str,
                        ))

                        state.add_step(StepResult(
                            step_number=step_idx,
                            thought=llm_response.content,
                            action_name=tool_call.name,
                            action_input=tool_call.arguments,
                            observation=obs_str,
                        ))

                    except Exception as err:
                        yield AgentEvent(
                            event_id=str(uuid.uuid4()),
                            event_type=AgentEventType.TOOL_ERROR,
                            step=step_idx,
                            payload={"tool": tool_call.name, "error": str(err)},
                        )
                        raise

            if not state.is_finished:
                msg = f"达到最大步数上限 ({self.config.max_steps}) 熔断退出。"
                yield AgentEvent(
                    event_id=str(uuid.uuid4()),
                    event_type=AgentEventType.ERROR,
                    step=state.current_step,
                    payload={"error": msg},
                )

        except Exception as e:
            for hook in self.hooks:
                await hook.on_error(context, e)
            yield AgentEvent(
                event_id=str(uuid.uuid4()),
                event_type=AgentEventType.ERROR,
                step=state.current_step,
                payload={"error": str(e)},
            )
