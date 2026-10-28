"""ReAct 执行引擎单元测试 (兼容内置 unittest 与 pytest)."""
import unittest
from react_ai.core.engine import ReActAgent
from react_ai.core.context import RunContext
from react_ai.tools.registry import ToolRegistry
from react_ai.tools.base import tool
from react_ai.llm.base import BaseLLMClient, LLMResponse
from react_ai.schemas.messages import ToolCall


class MockLLM(BaseLLMClient):
    def __init__(self):
        self.call_count = 0

    async def generate(self, messages, tools=None, temperature=0.2, max_tokens=None):
        self.call_count += 1
        if self.call_count == 1:
            return LLMResponse(
                content="我需要调用加法工具",
                tool_calls=[ToolCall(id="call_1", name="add", arguments={"a": 10, "b": 20})]
            )
        else:
            return LLMResponse(content="计算结果为 30。")

    async def stream_generate(self, messages, tools=None, temperature=0.2):
        yield "Mock response"


class TestReActEngine(unittest.IsolatedAsyncioTestCase):
    async def test_react_agent_full_cycle(self):
        @tool(name="add", description="两数相加")
        def add(a: int, b: int) -> int:
            return a + b

        registry = ToolRegistry()
        registry.register(add)

        agent = ReActAgent(llm=MockLLM(), tool_registry=registry)
        ctx = RunContext()

        response = await agent.run("请计算 10 + 20", ctx)
        self.assertTrue(response.success)
        self.assertEqual(response.final_output, "计算结果为 30。")
        self.assertEqual(len(response.steps), 2)
        self.assertEqual(response.steps[0].action_name, "add")
        self.assertEqual(response.steps[0].observation, "30")


if __name__ == "__main__":
    unittest.main()
