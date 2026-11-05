"""差距分析 2.6：租户强制隔离闸验收测试.

覆盖验收标准：
1. 启用闸时 tenant_id=None 的 read/write 直接被拒绝；
2. clear_session/clear_user 无 tenant_id 被拒绝；
3. 跨租户查询同 session_id / user_id 被正确隔离；
4. SessionKey 路径（2.2 已落地的 load_session/save_turn/save_working_state/save_tool_result）
   自带 tenant_id，天然不会触发闸；
5. 关闭闸后行为向后兼容；
6. 工厂透传 ENFORCE_TENANT_ISOLATION 配置。

全部使用 InMemoryMemory，零外部依赖。
"""

import sys
import types
import unittest
from unittest.mock import patch

from react_ai.config.settings import settings
from react_ai.memory.base import MemoryItem, MemoryQuery, MemoryType, SessionKey
from react_ai.memory.factory import create_memory_from_settings
from react_ai.memory.in_memory import InMemoryMemory


class TestWriteTenantGuard(unittest.TestCase):
    """write 入口的 tenant 硬闸."""

    def test_write_rejects_no_tenant(self):
        mem = InMemoryMemory(enforce_tenant_isolation=True)
        with self.assertRaises(ValueError) as ctx:
            mem.write(MemoryItem(key="k", value="v"))
        self.assertIn("tenant_id 为必填", str(ctx.exception))

    def test_write_accepts_with_tenant(self):
        mem = InMemoryMemory(enforce_tenant_isolation=True)
        item = mem.write(MemoryItem(key="k", value="v", tenant_id="t1"))
        self.assertEqual(item.tenant_id, "t1")

    def test_disabled_allows_no_tenant(self):
        mem = InMemoryMemory(enforce_tenant_isolation=False)
        item = mem.write(MemoryItem(key="k", value="v"))
        self.assertIsNotNone(item.id)


class TestReadTenantGuard(unittest.TestCase):
    """read 入口的 tenant 硬闸."""

    def test_read_rejects_no_tenant(self):
        mem = InMemoryMemory(enforce_tenant_isolation=True)
        with self.assertRaises(ValueError) as ctx:
            mem.read(MemoryQuery())
        self.assertIn("tenant_id 为必填", str(ctx.exception))

    def test_read_accepts_with_tenant(self):
        mem = InMemoryMemory(enforce_tenant_isolation=True)
        mem.write(MemoryItem(key="k", value="v", tenant_id="t1"))
        result = mem.read(MemoryQuery(tenant_id="t1"))
        self.assertEqual(result.total_count, 1)

    def test_disabled_allows_no_tenant(self):
        mem = InMemoryMemory(enforce_tenant_isolation=False)
        mem.write(MemoryItem(key="k", value="v"))
        result = mem.read(MemoryQuery())
        self.assertEqual(result.total_count, 1)


class TestCrossTenantIsolation(unittest.TestCase):
    """跨租户数据隔离：相同 session_id / user_id 不可互见."""

    def _populate(self, mem: InMemoryMemory):
        mem.write(MemoryItem(key="a", value=1, session_id="s1", user_id="u1", tenant_id="tA"))
        mem.write(MemoryItem(key="b", value=2, session_id="s1", user_id="u1", tenant_id="tB"))
        mem.write(MemoryItem(key="c", value=3, session_id="s1", user_id="u1", tenant_id="tA"))

    def test_read_tenant_a_only_sees_own(self):
        mem = InMemoryMemory(enforce_tenant_isolation=True)
        self._populate(mem)
        res_tA = mem.read(MemoryQuery(tenant_id="tA"))
        res_tB = mem.read(MemoryQuery(tenant_id="tB"))
        self.assertEqual(res_tA.total_count, 2)
        self.assertEqual(res_tB.total_count, 1)
        self.assertTrue(all(i.tenant_id == "tA" for i in res_tA.items))
        self.assertTrue(all(i.tenant_id == "tB" for i in res_tB.items))

    def test_sessionkey_load_session_is_naturally_isolated(self):
        """SessionKey 三元组写入的条目天然带 tenant_id，不会触发闸."""
        mem = InMemoryMemory(enforce_tenant_isolation=True)
        key_A = SessionKey.resolve("tA", "u1", "s1")
        key_B = SessionKey.resolve("tB", "u1", "s1")
        mem.save_turn(key_A, [
            # save_turn 内部 write 已打 tenant_id，不会触发闸
            __import__("react_ai.schemas.messages", fromlist=["ChatMessage", "MessageRole"]).ChatMessage(
                role=__import__("react_ai.schemas.messages", fromlist=["MessageRole"]).MessageRole.USER,
                content="hi",
            ),
        ])
        load_A = mem.load_session(key_A)
        load_B = mem.load_session(key_B)
        # 两边 session_id 都是 "s1"，但因 tenant 三元组隔离互不可见
        self.assertEqual(len(load_A), 1)
        self.assertEqual(len(load_B), 0)


class TestClearTenantGuard(unittest.TestCase):
    """clear_session / clear_user 的 tenant 硬闸."""

    def _populate(self, mem):
        mem.write(MemoryItem(key="k", value="v", session_id="s1", tenant_id="tA"))

    def test_clear_session_rejects_no_tenant(self):
        mem = InMemoryMemory(enforce_tenant_isolation=True)
        self._populate(mem)
        with self.assertRaises(ValueError):
            mem.clear_session("s1")

    def test_clear_session_requires_matching_tenant(self):
        mem = InMemoryMemory(enforce_tenant_isolation=True)
        self._populate(mem)
        # 用 tB 去清 tA 的 session —— 什么都删不到
        deleted = mem.clear_session("s1", tenant_id="tB")
        self.assertEqual(deleted, 0)
        # 用正确 tenant 清
        deleted = mem.clear_session("s1", tenant_id="tA")
        self.assertEqual(deleted, 1)

    def test_clear_user_rejects_no_tenant(self):
        mem = InMemoryMemory(enforce_tenant_isolation=True)
        mem.write(MemoryItem(key="k", value="v", user_id="u1", tenant_id="tA"))
        with self.assertRaises(ValueError):
            mem.clear_user("u1")

    def test_disabled_allows_no_tenant_clear(self):
        mem = InMemoryMemory(enforce_tenant_isolation=False)
        mem.write(MemoryItem(key="k", value="v", user_id="u1", tenant_id="tA"))
        deleted = mem.clear_user("u1")  # 关闸后允许不带 tenant
        self.assertEqual(deleted, 1)


class TestFactoryTenantIsolationWiring(unittest.TestCase):
    """ENFORCE_TENANT_ISOLATION 经工厂透传到实例."""

    def test_default_enabled(self):
        with patch.object(settings.memory, "enforce_tenant_isolation", True):
            mem = create_memory_from_settings()
        self.assertTrue(mem.enforce_tenant_isolation)

    def test_explicit_disabled(self):
        with patch.object(settings.memory, "enforce_tenant_isolation", False):
            mem = create_memory_from_settings()
        self.assertFalse(mem.enforce_tenant_isolation)

    def test_redis_receives_flag(self):
        sink: dict = {}
        module = types.ModuleType("react_ai.memory.redis")

        class FakeRedisMemory:
            def __init__(self, **kwargs):
                sink["flag"] = kwargs.get("enforce_tenant_isolation")

            def is_connected(self):
                return True

        module.RedisMemory = FakeRedisMemory
        with patch.object(settings.memory, "memory_type", "redis"), patch.object(
            settings.memory, "enforce_tenant_isolation", False
        ), patch.dict(sys.modules, {"react_ai.memory.redis": module}):
            create_memory_from_settings()
        self.assertFalse(sink["flag"])


class TestGetStatsExposesFlag(unittest.TestCase):
    def test_stats_includes_tenant_flag(self):
        mem = InMemoryMemory(enforce_tenant_isolation=True)
        stats = mem.get_stats()
        self.assertIn("enforce_tenant_isolation", stats)
        self.assertTrue(stats["enforce_tenant_isolation"])


if __name__ == "__main__":
    unittest.main()
