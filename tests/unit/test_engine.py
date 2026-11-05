"""ReAct 执行引擎单元测试 (兼容内置 unittest 与 pytest)."""
import unittest
from react_ai.core.engine import ReActAgent
from react_ai.core.context import RunContext
from react_ai.memory.in_memory import InMemoryMemory
from react_ai.memory.base import SessionKey
from react_ai.tools.registry import ToolRegistry
from react_ai.tools.base import tool
from react_ai.llm.base import BaseLLMClient, LLMResponse
from react_ai.schemas.messages import ChatMessage, MessageRole, ToolCall


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


class EchoMockLLM(BaseLLMClient):
    """记录每次 generate 收到的 messages，直接返回最终答复（无工具调用）。"""

    def __init__(self):
        self.received_messages: list[list[ChatMessage]] = []

    async def generate(self, messages, tools=None, temperature=0.2, max_tokens=None):
        self.received_messages.append(list(messages))
        return LLMResponse(content="好的，已收到。")

    async def stream_generate(self, messages, tools=None, temperature=0.2):
        yield "ok"


class TestReActMemoryIntegration(unittest.IsolatedAsyncioTestCase):
    """差距分析 2.1：核心引擎与 memory 模块打通的验收测试."""

    async def test_second_request_contains_first_turn(self):
        """同一 session_id 的第二次请求，LLM 收到的 messages 包含首轮对话."""
        llm = EchoMockLLM()
        agent = ReActAgent(llm=llm, tool_registry=ToolRegistry(), memory=InMemoryMemory())

        await agent.run("我的订单号是 A123", RunContext(session_id="sess-1"))
        await agent.run("我刚才说的订单号是多少", RunContext(session_id="sess-1"))

        second_call = llm.received_messages[-1]
        contents = [m.content or "" for m in second_call]
        self.assertTrue(any("订单号是 A123" in c for c in contents))
        self.assertTrue(any(m.role == MessageRole.ASSISTANT for m in second_call))

    async def test_state_messages_traceable_to_memory(self):
        """回写的会话历史可在 memory 模块中追溯."""
        memory = InMemoryMemory()
        agent = ReActAgent(llm=EchoMockLLM(), tool_registry=ToolRegistry(), memory=memory)

        await agent.run("测试内容", RunContext(session_id="sX"))

        history = memory.load_session(SessionKey.resolve(session_id="sX"))
        self.assertEqual(len(history), 2)
        self.assertEqual(history[0].role, MessageRole.USER)
        self.assertEqual(history[0].content, "测试内容")
        self.assertEqual(history[1].role, MessageRole.ASSISTANT)

    async def test_memory_dependency_injection_point(self):
        """核心引擎具备 memory 依赖注入点，缺省为 None 时行为与原逻辑一致."""
        llm = EchoMockLLM()
        agent = ReActAgent(llm=llm, tool_registry=ToolRegistry())
        self.assertIsNone(agent.memory)

        await agent.run("第一轮", RunContext(session_id="s1"))
        await agent.run("第二轮", RunContext(session_id="s1"))

        # 未注入 memory 时无历史加载
        contents = [m.content or "" for m in llm.received_messages[-1]]
        self.assertFalse(any("第一轮" in c for c in contents))

    async def test_session_isolation(self):
        """不同 session_id 的历史互不可见."""
        llm = EchoMockLLM()
        agent = ReActAgent(llm=llm, tool_registry=ToolRegistry(), memory=InMemoryMemory())

        await agent.run("会话一的内容", RunContext(session_id="s1"))
        await agent.run("会话二的内容", RunContext(session_id="s2"))
        await agent.run("继续", RunContext(session_id="s1"))

        contents = [m.content or "" for m in llm.received_messages[-1]]
        self.assertTrue(any("会话一" in c for c in contents))
        self.assertFalse(any("会话二" in c for c in contents))

    async def test_user_isolation(self):
        """相同 session_id 但不同 user_id 的历史互不可见."""
        llm = EchoMockLLM()
        agent = ReActAgent(llm=llm, tool_registry=ToolRegistry(), memory=InMemoryMemory())

        await agent.run("用户A的内容", RunContext(session_id="shared", user_id="userA"))
        await agent.run("用户B的请求", RunContext(session_id="shared", user_id="userB"))

        contents = [m.content or "" for m in llm.received_messages[-1]]
        self.assertFalse(any("用户A" in c for c in contents))

    async def test_failed_run_not_saved(self):
        """异常结束的 run 不回写记忆，避免污染历史."""
        memory = InMemoryMemory()

        class BrokenLLM(BaseLLMClient):
            async def generate(self, messages, tools=None, temperature=0.2, max_tokens=None):
                raise RuntimeError("LLM 故障")

            async def stream_generate(self, messages, tools=None, temperature=0.2):
                yield ""

        agent = ReActAgent(llm=BrokenLLM(), tool_registry=ToolRegistry(), memory=memory)
        events = [e async for e in agent.stream("会失败的请求", RunContext(session_id="sErr"))]

        self.assertTrue(any(e.event_type.value == "error" for e in events))
        self.assertEqual(memory.load_session(SessionKey.resolve(session_id="sErr")), [])


if __name__ == "__main__":
    unittest.main()
