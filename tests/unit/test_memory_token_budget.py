"""差距分析 2.5：会话历史 token 预算控制验收测试.

全部使用内存实现，零外部依赖（无 Redis / PG），本机可跑。
"""

import sys
import types
import unittest
from unittest.mock import patch

from react_ai.config.settings import settings
from react_ai.memory.base import SessionKey
from react_ai.memory.in_memory import InMemoryMemory
from react_ai.memory.factory import create_memory_from_settings
from react_ai.memory.short_term import WindowBufferMemory
from react_ai.schemas.messages import ChatMessage, MessageRole
from react_ai.utils.token_counter import (
    estimate_messages_tokens,
    trim_messages_to_token_budget,
)


def _msgs(*contents: str) -> list[ChatMessage]:
    """按内容构造 user 消息列表（内容顺序即时间正序）."""
    return [ChatMessage(role=MessageRole.USER, content=c) for c in contents]


class TestTrimMessagesToTokenBudget(unittest.TestCase):
    """裁剪函数本身的语义."""

    def test_within_budget_untouched(self):
        msgs = _msgs("短消息一", "短消息二")
        kept, dropped = trim_messages_to_token_budget(msgs, 1000)
        self.assertEqual(len(kept), 2)
        self.assertEqual(dropped, 0)

    def test_drops_oldest_first(self):
        msgs = _msgs("旧" * 300, "中" * 300, "新" * 10)
        budget = estimate_messages_tokens(msgs[1:])
        kept, dropped = trim_messages_to_token_budget(msgs, budget)
        self.assertEqual(dropped, 1)
        self.assertEqual(kept[0].content, "中" * 300)
        self.assertTrue(kept[-1].content.startswith("新"))
        self.assertLessEqual(estimate_messages_tokens(kept), budget)

    def test_always_keeps_newest_even_if_over_budget(self):
        long_text = "超" * 2000
        kept, dropped = trim_messages_to_token_budget(_msgs("a", long_text), 10)
        self.assertEqual(len(kept), 1)
        self.assertEqual(kept[0].content, long_text)
        self.assertEqual(dropped, 1)

    def test_non_positive_budget_disables_trimming(self):
        msgs = _msgs("x" * 100, "y" * 100)
        kept, dropped = trim_messages_to_token_budget(msgs, 0)
        self.assertEqual(len(kept), 2)
        self.assertEqual(dropped, 0)

    def test_empty_list(self):
        kept, dropped = trim_messages_to_token_budget([], 100)
        self.assertEqual(kept, [])
        self.assertEqual(dropped, 0)


class TestLoadSessionTokenBudget(unittest.TestCase):
    """BaseMemory.load_session 的预算裁剪与截断事件指标."""

    def _make_memory_with_history(self, budget: int | None) -> InMemoryMemory:
        memory = InMemoryMemory(history_token_budget=budget)
        key = SessionKey(tenant_id="t1", user_id="u1", session_id="s1")
        # 写入 10 轮长对话，总 token 远超预算
        for i in range(10):
            memory.save_turn(
                key,
                [
                    ChatMessage(role=MessageRole.USER, content=f"第{i}轮提问：" + "问" * 400),
                    ChatMessage(
                        role=MessageRole.ASSISTANT, content=f"第{i}轮回答：" + "答" * 400
                    ),
                ],
            )
        return memory

    def test_history_never_exceeds_budget(self):
        budget = 1000
        memory = self._make_memory_with_history(budget)
        key = SessionKey(tenant_id="t1", user_id="u1", session_id="s1")
        loaded = memory.load_session(key)
        self.assertTrue(loaded, "预算再小也必须保留最近消息")
        self.assertLessEqual(estimate_messages_tokens(loaded), budget)

    def test_keeps_most_recent_messages(self):
        memory = self._make_memory_with_history(1000)
        key = SessionKey(tenant_id="t1", user_id="u1", session_id="s1")
        loaded = memory.load_session(key)
        self.assertIn("第9轮回答", loaded[-1].content)  # 最新一条必须保留

    def test_budget_none_keeps_all(self):
        memory = self._make_memory_with_history(None)
        key = SessionKey(tenant_id="t1", user_id="u1", session_id="s1")
        loaded = memory.load_session(key)
        self.assertEqual(len(loaded), 20)  # 行为与改造前一致

    def test_call_level_budget_overrides_instance(self):
        memory = self._make_memory_with_history(None)
        key = SessionKey(tenant_id="t1", user_id="u1", session_id="s1")
        loaded = memory.load_session(key, token_budget=500)
        self.assertLessEqual(estimate_messages_tokens(loaded), 500)

    def test_truncation_event_metrics(self):
        memory = self._make_memory_with_history(1000)
        key = SessionKey(tenant_id="t1", user_id="u1", session_id="s1")
        stats_before = memory.get_stats()
        self.assertEqual(stats_before["token_truncated_events"], 0)
        memory.load_session(key)
        stats = memory.get_stats()
        self.assertEqual(stats["token_truncated_events"], 1)
        self.assertGreater(stats["token_truncated_messages"], 0)
        self.assertEqual(stats["history_token_budget"], 1000)

    def test_no_truncation_event_when_within_budget(self):
        memory = InMemoryMemory(history_token_budget=10000)
        key = SessionKey(tenant_id="t1", user_id="u1", session_id="s1")
        memory.save_turn(
            key,
            [
                ChatMessage(role=MessageRole.USER, content="你好"),
                ChatMessage(role=MessageRole.ASSISTANT, content="你好，有什么可以帮您？"),
            ],
        )
        memory.load_session(key)
        self.assertEqual(memory.get_stats()["token_truncated_events"], 0)


class TestWindowBufferMemoryBudget(unittest.TestCase):
    """WindowBufferMemory 条数窗口之后的 token 预算裁剪."""

    async def _run(self, token_budget: int | None):
        memory = WindowBufferMemory(k=10, token_budget=token_budget)
        for i in range(6):
            await memory.save_context("s1", f"第{i}轮：" + "问" * 400, f"答{i}：" + "答" * 400)
        return await memory.load_memory_variables("s1")

    def test_budget_trims_window(self):
        import asyncio

        loaded = asyncio.run(self._run(1000))
        self.assertLessEqual(estimate_messages_tokens(loaded), 1000)

    def test_none_budget_keeps_count_window(self):
        import asyncio

        loaded = asyncio.run(self._run(None))
        self.assertEqual(len(loaded), 12)  # 2*k=20 条内全部保留


class TestFactoryBudgetWiring(unittest.TestCase):
    """HISTORY_TOKEN_BUDGET 配置经工厂透传到实例."""

    def test_in_memory_receives_budget(self):
        with patch.object(settings.memory, "memory_type", "in_memory"), patch.object(
            settings.memory, "history_token_budget", 321
        ):
            memory = create_memory_from_settings()
        self.assertEqual(memory.history_token_budget, 321)

    def test_redis_receives_budget(self):
        sink: dict = {}
        module = types.ModuleType("react_ai.memory.redis")

        class FakeRedisMemory:
            def __init__(self, redis_url=None, session_ttl_seconds=None, **kwargs):
                sink["history_token_budget"] = kwargs.get("history_token_budget")

            def is_connected(self):
                return True

        module.RedisMemory = FakeRedisMemory
        with patch.object(settings.memory, "memory_type", "redis"), patch.object(
            settings.memory, "history_token_budget", 777
        ), patch.dict(sys.modules, {"react_ai.memory.redis": module}):
            create_memory_from_settings()
        self.assertEqual(sink["history_token_budget"], 777)


if __name__ == "__main__":
    unittest.main()
