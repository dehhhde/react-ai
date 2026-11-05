"""差距分析 2.2 验收测试：session 身份三元组绑定与多租户/多用户隔离。

覆盖验收标准：
1. 不同 tenant/user 下相同 session_id 字符串互不可见；
2. 越权访问他人会话返回 404；
3. 匿名请求落入独立隔离命名空间；
4. 裸 session_id 查询被拒绝；
5. 会话 TTL 生效，过期自动开新会话；
6. API 层强制 session_id（缺失 422），端到端多轮对话成立。

全部使用内存实现，不依赖 PostgreSQL / Redis（fakeredis 本机缺失）。
"""
import time
import unittest

from fastapi.testclient import TestClient

from api.main import app
from api.routes.agent import get_agent
from react_ai.core.engine import ReActAgent
from react_ai.core.context import RunContext
from react_ai.llm.base import BaseLLMClient, LLMResponse
from react_ai.memory.base import (
    ANONYMOUS_TENANT_ID,
    ANONYMOUS_USER_ID,
    SessionKey,
)
from react_ai.memory.in_memory import InMemoryMemory
from react_ai.schemas.messages import ChatMessage, MessageRole
from react_ai.tools.registry import ToolRegistry


class EchoLLM(BaseLLMClient):
    """记录每次 generate 收到的 messages，直接返回最终答复（无工具调用）。"""

    def __init__(self):
        self.received: list[list[ChatMessage]] = []

    async def generate(self, messages, tools=None, temperature=0.2, max_tokens=None):
        self.received.append(list(messages))
        return LLMResponse(content="已收到。")

    async def stream_generate(self, messages, tools=None, temperature=0.2):
        yield "ok"


class TestSessionKey(unittest.TestCase):
    """SessionKey 三元组校验（禁止裸 session_id）。"""

    def test_requires_all_three_fields(self):
        for bad in (
            dict(tenant_id="", user_id="u", session_id="s"),
            dict(tenant_id="t", user_id="  ", session_id="s"),
            dict(tenant_id="t", user_id="u", session_id=""),
            dict(tenant_id=None, user_id="u", session_id="s"),
        ):
            with self.assertRaises(ValueError):
                SessionKey(**bad)

    def test_resolve_requires_session_id(self):
        with self.assertRaises(ValueError):
            SessionKey.resolve(tenant_id="t", user_id="u", session_id="")
        with self.assertRaises(ValueError):
            SessionKey.resolve(tenant_id="t", user_id="u", session_id=None)

    def test_resolve_falls_back_to_anonymous_namespace(self):
        key = SessionKey.resolve(session_id="s1")
        self.assertEqual(key.tenant_id, ANONYMOUS_TENANT_ID)
        self.assertEqual(key.user_id, ANONYMOUS_USER_ID)
        self.assertEqual(key.session_id, "s1")

    def test_composite_is_identity_scoped(self):
        a = SessionKey("t1", "u1", "s1").composite()
        b = SessionKey("t2", "u1", "s1").composite()
        self.assertNotEqual(a, b)


class TestMemorySessionIsolation(unittest.TestCase):
    """memory 层按三元组隔离，且拒绝裸 session_id 读写。"""

    def setUp(self):
        self.memory = InMemoryMemory()

    def _turn(self, text: str) -> list[ChatMessage]:
        return [
            ChatMessage(role=MessageRole.USER, content=text),
            ChatMessage(role=MessageRole.ASSISTANT, content="回声:" + text),
        ]

    def test_same_session_id_isolated_across_identity(self):
        key_a = SessionKey("t1", "u1", "s1")
        key_b = SessionKey("t2", "u1", "s1")  # 同 user 不同 tenant
        key_c = SessionKey("t1", "u2", "s1")  # 同 tenant 不同 user

        self.memory.save_turn(key_a, self._turn("A 的内容"))

        self.assertEqual(len(self.memory.load_session(key_a)), 2)
        self.assertEqual(self.memory.load_session(key_b), [])
        self.assertEqual(self.memory.load_session(key_c), [])

    def test_bare_session_id_rejected(self):
        with self.assertRaises(TypeError):
            self.memory.load_session("s1")  # type: ignore[arg-type]
        with self.assertRaises(TypeError):
            self.memory.save_turn("s1", self._turn("x"))  # type: ignore[arg-type]

    def test_anonymous_namespace_isolated_from_authenticated(self):
        anon = SessionKey.resolve(session_id="shared")
        real = SessionKey.resolve(tenant_id="t1", user_id="u1", session_id="shared")

        self.memory.save_turn(anon, self._turn("匿名内容"))

        self.assertEqual(len(self.memory.load_session(anon)), 2)
        self.assertEqual(self.memory.load_session(real), [])

    def test_session_ttl_expires_history(self):
        memory = InMemoryMemory(session_ttl_seconds=0.02)
        key = SessionKey("t1", "u1", "s1")
        memory.save_turn(key, self._turn("临时"))
        self.assertEqual(len(memory.load_session(key)), 2)

        time.sleep(0.05)
        # TTL 过期后历史清空，会话自然从头开始
        self.assertEqual(memory.load_session(key), [])


class TestEngineIdentityBinding(unittest.IsolatedAsyncioTestCase):
    """引擎按三元组绑定会话：跨身份不可见，同身份多轮成立。"""

    async def asyncSetUp(self):
        self.llm = EchoLLM()
        self.memory = InMemoryMemory()
        self.agent = ReActAgent(
            llm=self.llm, tool_registry=ToolRegistry(), memory=self.memory
        )

    async def test_cross_user_same_session_id_not_leaked(self):
        await self.agent.run(
            "用户A的秘密", RunContext(session_id="shared", user_id="userA", tenant_id="t1")
        )
        await self.agent.run(
            "用户B的请求", RunContext(session_id="shared", user_id="userB", tenant_id="t1")
        )
        contents = [m.content or "" for m in self.llm.received[-1]]
        self.assertFalse(any("用户A" in c for c in contents))

    async def test_cross_tenant_same_session_id_not_leaked(self):
        await self.agent.run(
            "租户A的内容", RunContext(session_id="shared", user_id="u", tenant_id="tA")
        )
        await self.agent.run(
            "租户B的请求", RunContext(session_id="shared", user_id="u", tenant_id="tB")
        )
        contents = [m.content or "" for m in self.llm.received[-1]]
        self.assertFalse(any("租户A" in c for c in contents))


class TestAgentApiSession(unittest.TestCase):
    """API 层：强制 session_id、越权 404、端到端多轮。"""

    def setUp(self):
        self.llm = EchoLLM()
        self.memory = InMemoryMemory()
        self.stub_agent = ReActAgent(
            llm=self.llm, tool_registry=ToolRegistry(), memory=self.memory
        )
        app.dependency_overrides[get_agent] = lambda: self.stub_agent
        self.client = TestClient(app)

    def tearDown(self):
        app.dependency_overrides.clear()

    def test_chat_requires_session_id(self):
        resp = self.client.post("/api/v1/agent/chat", json={"query": "你好"})
        self.assertEqual(resp.status_code, 422)

    def test_multi_turn_over_api(self):
        body = {"session_id": "s1", "user_id": "u1", "tenant_id": "t1"}
        r1 = self.client.post("/api/v1/agent/chat", json={"query": "我的名字是 Bob", **body})
        self.assertEqual(r1.status_code, 200)
        r2 = self.client.post("/api/v1/agent/chat", json={"query": "我叫什么名字", **body})
        self.assertEqual(r2.status_code, 200)

        # 第二轮 LLM 收到的上下文应包含首轮内容
        contents = [m.content or "" for m in self.llm.received[-1]]
        self.assertTrue(any("Bob" in c for c in contents))

    def test_cross_identity_session_history_returns_404(self):
        self.memory.save_turn(
            SessionKey("t1", "u1", "s1"),
            [
                ChatMessage(role=MessageRole.USER, content="私密"),
                ChatMessage(role=MessageRole.ASSISTANT, content="回复"),
            ],
        )
        # 正确身份可见
        ok = self.client.get(
            "/api/v1/agent/sessions/s1/messages", params={"user_id": "u1", "tenant_id": "t1"}
        )
        self.assertEqual(ok.status_code, 200)
        self.assertEqual(len(ok.json()["messages"]), 2)

        # 他人身份越权 → 404（不泄露会话是否存在）
        denied = self.client.get(
            "/api/v1/agent/sessions/s1/messages", params={"user_id": "u2", "tenant_id": "t1"}
        )
        self.assertEqual(denied.status_code, 404)


if __name__ == "__main__":
    unittest.main()
