"""记忆系统单元测试: BaseMemory, InMemoryMemory, RedisMemory."""

import time
import unittest
from concurrent.futures import ThreadPoolExecutor

import fakeredis

from react_ai.memory.base import (
    MemoryItem,
    MemoryQuery,
    MemoryType,
)
from react_ai.memory.in_memory import InMemoryMemory
from react_ai.memory.redis import RedisMemory


class TestMemoryItem(unittest.TestCase):
    """测试 MemoryItem 基础属性与序列化功能。"""

    def test_default_values(self):
        item = MemoryItem(key="user_name", value="Alice")
        self.assertIsNotNone(item.id)
        self.assertEqual(len(item.id), 32)
        self.assertEqual(item.type, MemoryType.SESSION)
        self.assertEqual(item.key, "user_name")
        self.assertEqual(item.value, "Alice")
        self.assertFalse(item.is_expired())

    def test_expiration_logic(self):
        # 永不过期
        item_no_ttl = MemoryItem(key="k1", ttl_seconds=None)
        self.assertFalse(item_no_ttl.is_expired())

        # 未过期
        item_valid = MemoryItem(key="k2", ttl_seconds=100.0)
        self.assertFalse(item_valid.is_expired())

        # 已过期
        item_expired = MemoryItem(key="k3", ttl_seconds=0.01)
        time.sleep(0.02)
        self.assertTrue(item_expired.is_expired())

    def test_to_dict_and_from_dict(self):
        item = MemoryItem(
            key="chat",
            value={"role": "user", "text": "hello"},
            type=MemoryType.LONG_TERM,
            session_id="sess_abc",
            user_id="user_123",
            tenant_id="tenant_01",
            ttl_seconds=3600.0,
            metadata={"source": "wechat"},
        )
        data = item.to_dict()
        self.assertEqual(data["type"], "long_term")
        self.assertEqual(data["session_id"], "sess_abc")
        self.assertEqual(data["metadata"]["source"], "wechat")

        restored = MemoryItem.from_dict(data)
        self.assertEqual(restored.id, item.id)
        self.assertEqual(restored.type, MemoryType.LONG_TERM)
        self.assertEqual(restored.value, {"role": "user", "text": "hello"})
        self.assertEqual(restored.tenant_id, "tenant_01")


class TestInMemoryMemory(unittest.TestCase):
    """测试 InMemoryMemory 内存存储及增删改查、过期与淘汰。"""

    def setUp(self):
        self.memory = InMemoryMemory(name="test_in_memory", max_items=10)

    def test_write_and_read(self):
        item1 = self.memory.write(
            MemoryItem(
                key="h1",
                value="v1",
                session_id="s1",
                user_id="u1",
                tenant_id="t1",
            )
        )
        item2 = self.memory.write(
            MemoryItem(
                key="h2",
                value="v2",
                session_id="s1",
                user_id="u2",
                tenant_id="t1",
                type=MemoryType.WORKING,
            )
        )
        item3 = self.memory.write(
            MemoryItem(
                key="h3",
                value="v3",
                session_id="s2",
                user_id="u1",
                tenant_id="t2",
                type=MemoryType.LONG_TERM,
            )
        )

        # 1. session_id 过滤
        res_s1 = self.memory.read(MemoryQuery(session_id="s1"))
        self.assertEqual(res_s1.total_count, 2)
        self.assertIn(item1.id, [i.id for i in res_s1.items])
        self.assertIn(item2.id, [i.id for i in res_s1.items])

        # 2. type 过滤
        res_type = self.memory.read(MemoryQuery(type=MemoryType.LONG_TERM))
        self.assertEqual(res_type.total_count, 1)
        self.assertEqual(res_type.items[0].id, item3.id)

        # 3. 多条件复合过滤
        res_multi = self.memory.read(MemoryQuery(session_id="s1", user_id="u1"))
        self.assertEqual(res_multi.total_count, 1)
        self.assertEqual(res_multi.items[0].id, item1.id)

    def test_update_existing_item(self):
        item = self.memory.write(MemoryItem(key="original", value=1))
        initial_id = item.id
        self.assertEqual(self.memory.get_stats()["item_count"], 1)

        # 更新该 ID 的值
        item.value = 2
        item.key = "updated"
        self.memory.write(item)

        self.assertEqual(self.memory.get_stats()["item_count"], 1)
        stored = self.memory.get(initial_id)
        self.assertIsNotNone(stored)
        self.assertEqual(stored.value, 2)
        self.assertEqual(stored.key, "updated")

    def test_delete(self):
        item = self.memory.write(MemoryItem(key="del_me", value="bye"))
        self.assertEqual(self.memory.get_stats()["item_count"], 1)

        # 成功删除
        success = self.memory.delete(item.id)
        self.assertTrue(success)
        self.assertEqual(self.memory.get_stats()["item_count"], 0)
        self.assertIsNone(self.memory.get(item.id))

        # 重复删除返回 False
        self.assertFalse(self.memory.delete(item.id))

    def test_clear_session_and_user(self):
        self.memory.write(MemoryItem(key="a", session_id="s_alpha", user_id="u1"))
        self.memory.write(MemoryItem(key="b", session_id="s_alpha", user_id="u2"))
        self.memory.write(MemoryItem(key="c", session_id="s_beta", user_id="u1"))

        # 清空 s_alpha 会话
        cleared_session = self.memory.clear_session("s_alpha")
        self.assertEqual(cleared_session, 2)
        self.assertEqual(
            self.memory.read(MemoryQuery(session_id="s_alpha")).total_count, 0
        )
        self.assertEqual(
            self.memory.read(MemoryQuery(session_id="s_beta")).total_count, 1
        )

        # 清空 u1 用户
        cleared_user = self.memory.clear_user("u1")
        self.assertEqual(cleared_user, 1)
        self.assertEqual(self.memory.get_stats()["item_count"], 0)

    def test_ttl_filtering(self):
        # 写入一条很快过期的记忆
        self.memory.write(MemoryItem(key="fast_expire", value="temp", ttl_seconds=0.02))
        self.memory.write(MemoryItem(key="permanent", value="keep", ttl_seconds=None))

        time.sleep(0.04)

        # 默认过滤过期项
        res = self.memory.read(MemoryQuery())
        self.assertEqual(res.total_count, 1)
        self.assertEqual(res.items[0].key, "permanent")

        # 显式包含过期项
        res_all = self.memory.read(MemoryQuery(include_expired=True))
        self.assertEqual(res_all.total_count, 2)

    def test_eviction_strategy(self):
        # 容量限制为 5
        mem = InMemoryMemory(name="small_mem", max_items=5, eviction_policy="lru")
        for i in range(5):
            mem.write(MemoryItem(key=f"k_{i}", value=i))

        self.assertEqual(mem.get_stats()["item_count"], 5)

        # 写入第 6 条触发容量超限并触发淘汰
        mem.write(MemoryItem(key="k_overflow", value=999))

        stats = mem.get_stats()
        # 淘汰后容量降至 <= 5 (降至 90% 即 4 条)
        self.assertLessEqual(stats["item_count"], 5)

    def test_thread_safety(self):
        def worker(idx):
            for j in range(20):
                self.memory.write(
                    MemoryItem(key=f"k_{idx}_{j}", value=j, session_id=f"sess_{idx}")
                )

        with ThreadPoolExecutor(max_workers=5) as executor:
            futures = [executor.submit(worker, i) for i in range(5)]
            for f in futures:
                f.result()

        stats = self.memory.get_stats()
        # 写入共 100 条，max_items 为 10，多次淘汰后应维持在 <= 10
        self.assertLessEqual(stats["item_count"], 10)


class TestRedisMemory(unittest.TestCase):
    """测试 RedisMemory 分布式存储实现 (基于 fakeredis 模拟隔离环境)."""

    def setUp(self):
        self.fake_client = fakeredis.FakeRedis(decode_responses=True)
        self.memory = RedisMemory(
            client=self.fake_client,
            key_prefix="unit_test:mem:",
            max_items=10,
        )

    def test_write_and_read(self):
        self.memory.write(
            MemoryItem(
                key="history",
                value={"msg": "hello"},
                session_id="sess_100",
                user_id="user_200",
                tenant_id="tenant_300",
                type=MemoryType.SESSION,
            )
        )
        item2 = self.memory.write(
            MemoryItem(
                key="pref",
                value={"theme": "dark"},
                session_id="sess_100",
                user_id="user_200",
                tenant_id="tenant_300",
                type=MemoryType.LONG_TERM,
            )
        )
        item3 = self.memory.write(
            MemoryItem(
                key="task",
                value="running",
                session_id="sess_101",
                user_id="user_201",
                tenant_id="tenant_300",
                type=MemoryType.WORKING,
            )
        )

        # 1. 验证按 session_id 过滤
        res = self.memory.read(MemoryQuery(session_id="sess_100"))
        self.assertEqual(res.total_count, 2)
        keys = [it.key for it in res.items]
        self.assertIn("history", keys)
        self.assertIn("pref", keys)

        # 2. 验证按 type 过滤
        res_type = self.memory.read(MemoryQuery(type=MemoryType.LONG_TERM))
        self.assertEqual(res_type.total_count, 1)
        self.assertEqual(res_type.items[0].id, item2.id)

        res_working = self.memory.read(MemoryQuery(type=MemoryType.WORKING))
        self.assertEqual(res_working.total_count, 1)
        self.assertEqual(res_working.items[0].id, item3.id)

    def test_update_existing_item(self):
        item = self.memory.write(MemoryItem(key="count", value=10))
        item_id = item.id

        # 更新值
        item.value = 20
        self.memory.write(item)

        fetched = self.memory.get(item_id)
        self.assertIsNotNone(fetched)
        self.assertEqual(fetched.value, 20)

    def test_delete(self):
        item = self.memory.write(MemoryItem(key="temp", value="abc"))
        self.assertTrue(self.memory.delete(item.id))
        self.assertIsNone(self.memory.get(item.id))

    def test_clear_session_and_user(self):
        self.memory.write(MemoryItem(key="s1_m1", session_id="s1", user_id="u1"))
        self.memory.write(MemoryItem(key="s1_m2", session_id="s1", user_id="u2"))
        self.memory.write(MemoryItem(key="s2_m1", session_id="s2", user_id="u1"))

        cleared_session = self.memory.clear_session("s1")
        self.assertEqual(cleared_session, 2)
        self.assertEqual(self.memory.read(MemoryQuery(session_id="s1")).total_count, 0)
        self.assertEqual(self.memory.read(MemoryQuery(session_id="s2")).total_count, 1)

        cleared_user = self.memory.clear_user("u1")
        self.assertEqual(cleared_user, 1)
        self.assertEqual(self.memory.read(MemoryQuery(user_id="u1")).total_count, 0)

    def test_native_ttl(self):
        self.memory.write(MemoryItem(key="ttl_fast", value="exp", ttl_seconds=0.03))
        self.memory.write(MemoryItem(key="ttl_none", value="keep", ttl_seconds=None))

        time.sleep(0.06)

        res = self.memory.read(MemoryQuery())
        self.assertEqual(res.total_count, 1)
        self.assertEqual(res.items[0].key, "ttl_none")

    def test_dynamic_configure(self):
        # 验证稍后配置连接的机制
        mem = RedisMemory(client=None, lazy_connect=True)
        self.assertIsNone(mem._client)

        # 动态注入配置 (注入 fake client)
        mem.configure(client=self.fake_client)
        self.assertIsNotNone(mem._client)
        self.assertTrue(mem.is_connected())

        item = mem.write(MemoryItem(key="after_config", value="ok"))
        self.assertEqual(mem.get(item.id).value, "ok")

    def test_eviction(self):
        mem = RedisMemory(
            client=self.fake_client,
            key_prefix="unit_test:evict:",
            max_items=4,
        )
        for i in range(4):
            mem.write(MemoryItem(key=f"k_{i}", value=i))

        # 写入第 5 条触发超限淘汰
        mem.write(MemoryItem(key="k_overflow", value=999))
        stats = mem.get_stats()
        self.assertLessEqual(stats["item_count"], 4)


if __name__ == "__main__":
    unittest.main()
