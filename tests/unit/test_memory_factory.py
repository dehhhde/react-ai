"""差距分析 2.4：配置驱动的记忆装配工厂验收测试.

本机无 Redis 服务且未安装 redis 包，全部用假模块/假实现替代，禁止真实连接。
"""

import sys
import types
import unittest
from unittest.mock import patch

from react_ai.config.settings import settings
from react_ai.memory.factory import create_memory_from_settings
from react_ai.memory.in_memory import InMemoryMemory


def _fake_redis_module(connected: bool, sink: dict) -> types.ModuleType:
    """构造假的 react_ai.memory.redis 模块，记录构造参数，不发起真实连接."""
    module = types.ModuleType("react_ai.memory.redis")

    class FakeRedisMemory:
        def __init__(self, redis_url=None, session_ttl_seconds=None, **kwargs):
            sink["redis_url"] = redis_url
            sink["session_ttl_seconds"] = session_ttl_seconds

        def is_connected(self):
            return connected

    module.RedisMemory = FakeRedisMemory
    return module


class TestMemoryFactory(unittest.TestCase):
    """MEMORY_TYPE / SESSION_TTL 配置驱动装配与降级链路."""

    def test_default_builds_in_memory_with_ttl(self):
        with patch.object(settings.memory, "memory_type", "in_memory"), patch.object(
            settings.memory, "session_ttl_seconds", 1234
        ):
            memory = create_memory_from_settings()
        self.assertIsInstance(memory, InMemoryMemory)
        self.assertEqual(memory.name, "agent_session_memory")
        self.assertEqual(memory.session_ttl_seconds, 1234.0)

    def test_unknown_type_falls_back_to_in_memory(self):
        with patch.object(settings.memory, "memory_type", "mysql"):
            memory = create_memory_from_settings()
        self.assertIsInstance(memory, InMemoryMemory)

    def test_redis_selected_when_connected(self):
        sink: dict = {}
        with patch.object(settings.memory, "memory_type", "redis"), patch.object(
            settings.memory, "redis_url", "redis://127.0.0.1:6399/2"
        ), patch.object(settings.memory, "session_ttl_seconds", 60), patch.dict(
            sys.modules, {"react_ai.memory.redis": _fake_redis_module(True, sink)}
        ):
            memory = create_memory_from_settings()
        self.assertEqual(type(memory).__name__, "FakeRedisMemory")
        self.assertEqual(sink["redis_url"], "redis://127.0.0.1:6399/2")
        self.assertEqual(sink["session_ttl_seconds"], 60.0)

    def test_redis_url_resolved_from_host_port_password(self):
        sink: dict = {}
        with patch.object(settings.memory, "memory_type", "redis"), patch.object(
            settings.memory, "redis_url", None
        ), patch.object(settings.memory, "host", "cache.internal"), patch.object(
            settings.memory, "port", 6390
        ), patch.object(
            settings.memory, "password", "s3cret"
        ), patch.object(
            settings.memory, "db", 1
        ), patch.dict(
            sys.modules, {"react_ai.memory.redis": _fake_redis_module(True, sink)}
        ):
            create_memory_from_settings()
        self.assertEqual(sink["redis_url"], "redis://:s3cret@cache.internal:6390/1")

    def test_redis_unreachable_falls_back_to_in_memory(self):
        sink: dict = {}
        with patch.object(settings.memory, "memory_type", "redis"), patch.dict(
            sys.modules, {"react_ai.memory.redis": _fake_redis_module(False, sink)}
        ):
            memory = create_memory_from_settings()
        self.assertIsInstance(memory, InMemoryMemory)

    def test_redis_package_missing_falls_back_to_in_memory(self):
        # sys.modules 置 None 使 from react_ai.memory.redis import ... 抛 ImportError，
        # 模拟本机未安装 redis 包的场景
        with patch.object(settings.memory, "memory_type", "redis"), patch.dict(
            sys.modules, {"react_ai.memory.redis": None}
        ):
            memory = create_memory_from_settings()
        self.assertIsInstance(memory, InMemoryMemory)


class TestAgentWiring(unittest.TestCase):
    """get_agent() 必须经由工厂装配记忆，而非写死实现."""

    def test_get_agent_uses_memory_factory(self):
        from api.routes import agent as agent_module

        sentinel = InMemoryMemory(name="from_factory")
        with patch.object(
            agent_module, "create_memory_from_settings", return_value=sentinel
        ) as factory, patch.object(agent_module, "create_llm_client", return_value=None), patch.object(
            agent_module, "get_registry", return_value=None
        ), patch.object(
            agent_module, "get_approval_service", return_value=None
        ), patch.object(
            agent_module, "_agent", None
        ):
            agent = agent_module.get_agent()
        factory.assert_called_once()
        self.assertIs(agent.memory, sentinel)


if __name__ == "__main__":
    unittest.main()
