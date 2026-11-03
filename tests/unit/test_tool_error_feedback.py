"""工具失败回环测试：错误 Observation 回喂模型、retryable 自动重试、连续失败熔断."""
import unittest
from typing import List

from react_ai.approval.service import reset_approval_service
from react_ai.core.checkpoint import InMemoryCheckpointStore
from react_ai.core.context import RunContext
from react_ai.core.engine import ReActAgent
from react_ai.idempotency.guard import reset_idempotency_guard
from react_ai.llm.base import BaseLLMClient, LLMResponse
from react_ai.schemas.agent import AgentConfig
from react_ai.schemas.events import AgentEventType
from react_ai.schemas.messages import MessageRole, ToolCall
from react_ai.tools.base import BaseTool, ToolContext
from react_ai.tools.registry import ToolRegistry


class ConfigurableFailTool(BaseTool):
    """可配置失败类型的工具，用于测试错误回环."""

    name = "configurable_fail_tool"
    description = "测试工具"
    side_effects = "none"

    def __init__(self, error_type="schema", retryable=False):
        self.error_type = error_type
        self.retryable = retryable
        self.calls = 0

    @property
    def input_schema(self) -> dict:
        return {"type": "object", "properties": {}, "required": []}

    def execute(self, arguments, ctx):
        self.calls += 1
        result = self._fail(
            self.error_type,
            f"error_{self.error_type}",
            f"模拟{self.error_type}错误",
            ctx.trace_id,
        )
        result.retryable = self.retryable
        return result


class EventuallySucceedsTool(BaseTool):
    """前 N 次返回 retryable 错误，之后成功."""

    name = "eventually_succeeds_tool"
    description = "测试工具"
    side_effects = "none"

    def __init__(self, fail_times: int = 1):
        self.fail_times = fail_times
        self.calls = 0

    @property
    def input_schema(self) -> dict:
        return {"type": "object", "properties": {}, "required": []}

    def execute(self, arguments, ctx):
        self.calls += 1
        if self.calls <= self.fail_times:
            return self._fail("external", "transient", "瞬时故障", ctx.trace_id)
        return {"ok_data": True}


class RepeatingCallLLM(BaseLLMClient):
    """只要还没看到最终答复，就一直调用指定工具；否则输出最终答复."""

    def __init__(self, tool_name: str, tool_call_id: str = "call_1"):
        self.tool_name = tool_name
        self.tool_call_id = tool_call_id
        self.calls = 0

    async def generate(self, messages, tools=None, temperature=0.2, max_tokens=None, tool_choice=None):
        self.calls += 1
        has_tool_msg = any(m.role == MessageRole.TOOL for m in messages)
        if not has_tool_msg:
            return LLMResponse(
                content="调用工具",
                tool_calls=[ToolCall(id=self.tool_call_id, name=self.tool_name, arguments={})],
            )
        return LLMResponse(content=f"已收到工具反馈，任务结束。调用次数: {self.calls}")

    async def stream_generate(self, messages, tools=None, temperature=0.2):
        yield "mock"


class AlwaysCallToolLLM(BaseLLMClient):
    """无论何时都调用工具，用于触发连续失败熔断."""

    def __init__(self, tool_name: str):
        self.tool_name = tool_name
        self.calls = 0

    async def generate(self, messages, tools=None, temperature=0.2, max_tokens=None, tool_choice=None):
        self.calls += 1
        return LLMResponse(
            content="继续调用",
            tool_calls=[ToolCall(id=f"call_{self.calls}", name=self.tool_name, arguments={})],
        )

    async def stream_generate(self, messages, tools=None, temperature=0.2):
        yield "mock"


def build_agent(tools: List[BaseTool], config: AgentConfig, llm: BaseLLMClient):
    registry = ToolRegistry()
    for t in tools:
        registry.register(t)
    return ReActAgent(
        llm=llm,
        tool_registry=registry,
        config=config,
        approval_service=reset_approval_service(),
        checkpoint_store=InMemoryCheckpointStore(),
    )


def collect_events(agen):
    import asyncio
    return asyncio.run(_collect(agen))


async def _collect(agen):
    events = []
    async for e in agen:
        events.append(e)
    return events


class TestToolErrorFeedback(unittest.TestCase):
    def setUp(self):
        reset_idempotency_guard()
        reset_approval_service()

    def test_schema_error_fed_back_to_model(self):
        tool = ConfigurableFailTool(error_type="schema", retryable=False)
        config = AgentConfig()
        agent = build_agent([tool], config, RepeatingCallLLM(tool.name))

        # run() 是协程，需在事件循环中执行
        import asyncio
        response = asyncio.run(agent.run("测试", RunContext()))

        self.assertTrue(response.success)
        # 工具执行被回喂，模型看到错误 Observation 后给出最终答复
        self.assertIn("已收到工具反馈", response.final_output)
        # 仅执行一次（schema 错误不重试）
        self.assertEqual(tool.calls, 1)

    def test_business_error_fed_back(self):
        import asyncio
        tool = ConfigurableFailTool(error_type="business", retryable=False)
        agent = build_agent([tool], AgentConfig(), RepeatingCallLLM(tool.name))
        response = asyncio.run(agent.run("测试", RunContext()))
        self.assertTrue(response.success)
        self.assertEqual(tool.calls, 1)

    def test_retryable_error_auto_retries(self):
        import asyncio
        tool = EventuallySucceedsTool(fail_times=1)
        config = AgentConfig(tool_retry_attempts=3)
        agent = build_agent([tool], config, RepeatingCallLLM(tool.name))
        response = asyncio.run(agent.run("测试", RunContext()))
        self.assertTrue(response.success)
        # 首次失败 + 重试成功 = 2 次调用
        self.assertEqual(tool.calls, 2)
        self.assertIn("已收到工具反馈", response.final_output)

    def test_consecutive_failures_circuit_breaker(self):
        import asyncio
        tool = ConfigurableFailTool(error_type="external", retryable=False)
        config = AgentConfig(max_consecutive_tool_failures=2)
        agent = build_agent([tool], config, AlwaysCallToolLLM(tool.name))
        response = asyncio.run(agent.run("测试", RunContext()))
        # 连续失败达阈值 → 熔断，run 失败
        self.assertFalse(response.success)
        self.assertIn("熔断", response.error_message)

    def test_success_resets_failure_counter(self):
        """失败后成功再失败：计数器归零，不会因累积而熔断."""
        import asyncio

        class FailThenSuccessThenFailTool(BaseTool):
            name = "fstf_tool"
            description = "测试工具"
            side_effects = "none"

            def __init__(self):
                self.calls = 0

            @property
            def input_schema(self):
                return {"type": "object", "properties": {}, "required": []}

            def execute(self, arguments, ctx):
                self.calls += 1
                if self.calls == 1:
                    return self._fail("schema", "e", "首次失败", ctx.trace_id)
                if self.calls == 2:
                    return {"data": "success"}
                return self._fail("schema", "e", "再次失败", ctx.trace_id)

        class ThreeCallLLM(BaseLLMClient):
            """固定调用工具 3 次后给出最终答复，用于验证计数器归零."""

            def __init__(self, tool_name: str):
                self.tool_name = tool_name
                self.calls = 0

            async def generate(self, messages, tools=None, temperature=0.2, max_tokens=None, tool_choice=None):
                self.calls += 1
                tool_msgs = [m for m in messages if m.role == MessageRole.TOOL]
                if len(tool_msgs) < 3:
                    return LLMResponse(
                        content="继续调用",
                        tool_calls=[ToolCall(id=f"call_{self.calls}", name=self.tool_name, arguments={})],
                    )
                return LLMResponse(content="已完成测试")

            async def stream_generate(self, messages, tools=None, temperature=0.2):
                yield "mock"

        tool = FailThenSuccessThenFailTool()
        config = AgentConfig(max_consecutive_tool_failures=2)
        agent = build_agent([tool], config, ThreeCallLLM(tool.name))
        response = asyncio.run(agent.run("测试", RunContext()))
        # 调用序列: 失败(计数1) → 成功(归零) → 失败(计数1) → 模型最终答复
        # 计数器从未达到阈值 2 → 不熔断
        self.assertTrue(response.success)
        self.assertEqual(tool.calls, 3)

    def test_error_observation_carries_error_type(self):
        import asyncio
        tool = ConfigurableFailTool(error_type="auth", retryable=False)
        agent = build_agent([tool], AgentConfig(), RepeatingCallLLM(tool.name))
        events = collect_events(agent.stream("测试", RunContext()))
        obs_events = [e for e in events if e.event_type == AgentEventType.OBSERVATION]
        self.assertEqual(len(obs_events), 1)
        self.assertIn("权限不足", obs_events[0].payload["observation"])
        self.assertIn("转人工", obs_events[0].payload["observation"])


if __name__ == "__main__":
    unittest.main()
