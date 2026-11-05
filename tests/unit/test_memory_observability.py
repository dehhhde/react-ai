"""差距分析 2.8：记忆路径可观测验收测试.

覆盖验收标准：
1. 链路中可见 memory.load / memory.save span（含 memory.read/write/search/delete/clear）；
2. /metrics 输出含 memory_* 指标（操作计数、耗时、条目数、命中率、淘汰数、后端健康度）；
3. 记忆读空 → miss，读到数据 → hit，命中率可聚合；
4. 后端异常时健康度位下降，恢复后回升；
5. span 属性只带租户/会话维度，不带用户内容原文（避免敏感信息进链路）。

全部使用内存实现，零外部依赖（无 Redis / PG）。
"""

import asyncio
import unittest

from fastapi.testclient import TestClient

from api.main import app
from react_ai.memory.base import MemoryItem, MemoryQuery, SessionKey
from react_ai.memory.in_memory import InMemoryMemory
from react_ai.memory.long_term import LongTermSemanticMemory
from react_ai.observability import metrics, tracer
from react_ai.schemas.messages import ChatMessage, MessageRole


def _turn(text: str) -> list[ChatMessage]:
    return [
        ChatMessage(role=MessageRole.USER, content=text),
        ChatMessage(role=MessageRole.ASSISTANT, content=f"回复：{text}"),
    ]


class _MetricsResetMixin:
    """每个用例前清空全局指标与 span，避免用例间串扰."""

    def setUp(self):
        metrics.reset()
        tracer.reset()

    def tearDown(self):
        metrics.reset()
        tracer.reset()


class TestMemorySpans(_MetricsResetMixin, unittest.TestCase):
    """链路追踪：记忆读写产生可检索的 span."""

    def test_save_turn_emits_memory_save_span(self):
        mem = InMemoryMemory(name="in_memory")
        mem.save_turn(SessionKey("t1", "u1", "s1"), _turn("你好"))
        spans = tracer.recent_spans()
        names = [s["name"] for s in spans]
        self.assertIn("memory.save", names)
        save_span = next(s for s in spans if s["name"] == "memory.save")
        self.assertEqual(save_span["status"], "ok")
        self.assertEqual(save_span["attributes"]["tenant_id"], "t1")
        self.assertEqual(save_span["attributes"]["saved_count"], 2)

    def test_load_session_emits_memory_load_span(self):
        mem = InMemoryMemory(name="in_memory")
        key = SessionKey("t1", "u1", "s1")
        mem.save_turn(key, _turn("你好"))
        mem.load_session(key)
        load_span = next(s for s in tracer.recent_spans() if s["name"] == "memory.load")
        self.assertEqual(load_span["status"], "ok")
        self.assertEqual(load_span["attributes"]["message_count"], 2)
        self.assertEqual(load_span["attributes"]["backend"], "in_memory")

    def test_read_and_write_emits_spans(self):
        mem = InMemoryMemory()
        mem.write(MemoryItem(key="k", value="v", tenant_id="t1"))
        mem.read(MemoryQuery(tenant_id="t1"))
        names = [s["name"] for s in tracer.recent_spans()]
        self.assertIn("memory.write", names)
        self.assertIn("memory.read", names)

    def test_span_does_not_leak_message_content(self):
        """span 属性只保留身份与统计维度，不得含用户原文."""
        mem = InMemoryMemory()
        secret = "我的身份证号是 110101199001011234"
        mem.save_turn(SessionKey("t1", "u1", "s1"), _turn(secret))
        for span in tracer.recent_spans():
            if not span["name"].startswith("memory."):
                continue
            dumped = str(span["attributes"])
            self.assertNotIn("身份证号", dumped)
            self.assertNotIn(secret, dumped)

    def test_failed_tenant_guard_emits_error_span(self):
        mem = InMemoryMemory(enforce_tenant_isolation=True)
        with self.assertRaises(ValueError):
            mem.read(MemoryQuery())
        span = next(s for s in tracer.recent_spans() if s["name"] == "memory.read")
        self.assertEqual(span["status"], "error")


class TestMemoryMetrics(_MetricsResetMixin, unittest.TestCase):
    """指标：计数 / 命中率 / 淘汰 / 后端健康度."""

    def test_ops_counter(self):
        mem = InMemoryMemory()
        key = SessionKey("t1", "u1", "s1")
        mem.save_turn(key, _turn("你好"))
        mem.load_session(key)
        ops = metrics.snapshot()["memory_ops"]
        keys = " ".join(ops.keys())
        self.assertIn("op=save", keys)
        self.assertIn("op=load", keys)
        self.assertIn("tenant=t1", keys)

    def test_hit_rate_miss_then_hit(self):
        mem = InMemoryMemory()
        key = SessionKey("t1", "u1", "s1")
        mem.load_session(key)  # 空历史 → miss
        mem.save_turn(key, _turn("你好"))
        mem.load_session(key)  # 有历史 → hit
        hit_rate = metrics.snapshot()["memory_hit_rate"]
        entry = next(v for k, v in hit_rate.items() if "op=load" in k)
        self.assertEqual(entry["misses"], 1)
        self.assertEqual(entry["hits"], 1)
        self.assertAlmostEqual(entry["hit_rate"], 0.5)

    def test_backend_health_drops_on_error_and_recovers(self):
        mem = InMemoryMemory(name="in_memory")
        with self.assertRaises(ValueError):
            mem.read(MemoryQuery())
        self.assertEqual(metrics.snapshot()["memory_backend_health"]["in_memory"], 0)
        mem.write(MemoryItem(key="k", value="v", tenant_id="t1"))
        self.assertEqual(metrics.snapshot()["memory_backend_health"]["in_memory"], 1)

    def test_evictions_counted(self):
        mem = InMemoryMemory(max_items=2)
        for i in range(6):
            mem.write(MemoryItem(key=f"k{i}", value=i, tenant_id="t1"))
        self.assertGreaterEqual(metrics.snapshot()["memory_evictions"]["in_memory"], 1)

    def test_long_term_search_hit_miss(self):
        async def scenario():
            lt = LongTermSemanticMemory(vector_index_name="lt_idx", tenant_id="t1")
            await lt.add_fact("u1", "用户偏好顺丰快递")
            await lt.search_relevant_facts("u1", "快递偏好")
            await lt.search_relevant_facts("u2", "快递偏好")  # 无命中
        asyncio.run(scenario())
        hit_rate = metrics.snapshot()["memory_hit_rate"]
        entry = next(v for k, v in hit_rate.items() if "op=search" in k)
        self.assertGreaterEqual(entry["hits"], 1)
        self.assertGreaterEqual(entry["misses"], 1)


class TestMetricsEndpoint(_MetricsResetMixin, unittest.TestCase):
    """HTTP 面：/metrics 暴露 memory_* 指标."""

    def test_prometheus_output_contains_memory_metrics(self):
        mem = InMemoryMemory(name="in_memory")
        key = SessionKey("t1", "u1", "s1")
        mem.save_turn(key, _turn("你好"))
        mem.load_session(key)

        client = TestClient(app)
        resp = client.get("/api/v1/observability/metrics")
        self.assertEqual(resp.status_code, 200)
        body = resp.text
        for metric in (
            "react_ai_memory_ops_total",
            "react_ai_memory_op_duration_ms_count",
            "react_ai_memory_items_total",
            "react_ai_memory_evictions_total",
            "react_ai_memory_backend_up",
        ):
            self.assertIn(metric, body)
        self.assertIn('op="load"', body)
        self.assertIn('op="save"', body)

    def test_metrics_snapshot_exposes_memory_sections(self):
        client = TestClient(app)
        resp = client.get("/api/v1/observability/metrics/snapshot")
        self.assertEqual(resp.status_code, 200)
        payload = resp.json()
        self.assertIn("memory_ops", payload)
        self.assertIn("memory_hit_rate", payload)
        self.assertIn("memory_backend_health", payload)


if __name__ == "__main__":
    unittest.main()
