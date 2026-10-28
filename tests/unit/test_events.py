"""BaseEvent 与事件模型单元测试."""
import json
import time
import unittest

from react_ai.events.base import (
    BaseEvent,
    EventContext,
    EventEnvelope,
    EventHeader,
    EventLevel,
    EventPayload,
    EventScope,
)


class TestEventModels(unittest.TestCase):
    def test_envelope_serialize_and_deserialize(self):
        header = EventHeader(
            trace_id="trace-123",
            span_id="span-456",
            parent_event_id="parent-789",
            source="test_source",
            version="1.1",
            retry_count=1,
            max_retries=5,
        )
        payload = EventPayload(
            event_type="order.created",
            data={"order_id": "OD1001", "amount": 99.5},
            metadata={"source_ip": "127.0.0.1"},
            schema_version="2.0",
        )
        context = EventContext(
            tenant_id="tenant-a",
            user_id="user-001",
            session_id="session-xyz",
            agent_id="agent-aftersales",
            task_id="task-1",
            environment="staging",
            ip_address="192.168.1.1",
            user_agent="pytest-client",
        )
        envelope = EventEnvelope(
            header=header,
            payload=payload,
            context=context,
            level=EventLevel.WARNING,
            scope=EventScope.CLUSTER,
            tags=["finance", "order"],
        )

        serialized_str = envelope.serialize()
        self.assertIsInstance(serialized_str, str)
        self.assertEqual(envelope.serialized, serialized_str)

        # 反序列化还原
        deserialized = EventEnvelope.deserialize(serialized_str)
        self.assertEqual(deserialized.header.event_id, header.event_id)
        self.assertEqual(deserialized.header.trace_id, "trace-123")
        self.assertEqual(deserialized.payload.event_type, "order.created")
        self.assertEqual(deserialized.payload.data["order_id"], "OD1001")
        self.assertEqual(deserialized.context.tenant_id, "tenant-a")
        self.assertEqual(deserialized.level, EventLevel.WARNING)
        self.assertEqual(deserialized.scope, EventScope.CLUSTER)
        self.assertEqual(deserialized.tags, ["finance", "order"])

    def test_deserialize_fallback_defaults(self):
        # 兼容缺失可选字段的旧结构
        minimal_json = json.dumps({
            "header": {
                "event_id": "test-id-1",
                "timestamp": time.time(),
                "source": "test",
            },
            "payload": {
                "event_type": "agent.ping",
            },
        })
        envelope = EventEnvelope.deserialize(minimal_json)
        self.assertEqual(envelope.header.event_id, "test-id-1")
        self.assertEqual(envelope.payload.event_type, "agent.ping")
        self.assertEqual(envelope.level, EventLevel.INFO)
        self.assertEqual(envelope.scope, EventScope.LOCAL)


class CustomAuditedEventBus(BaseEvent):
    """自定义子类，验证生命周期钩子."""
    def __init__(self, name="audit_bus"):
        super().__init__(name=name)
        self.before_published_events = []
        self.after_published_events = []
        self.captured_errors = []

    def _before_publish(self, envelope: EventEnvelope):
        self.before_published_events.append(envelope)

    def _after_publish(self, envelope: EventEnvelope):
        self.after_published_events.append(envelope)

    def _on_handler_error(self, handler, envelope: EventEnvelope, error: Exception):
        self.captured_errors.append((handler, envelope, error))


class TestBaseEvent(unittest.TestCase):
    def setUp(self):
        self.bus = BaseEvent(name="test_bus")

    def test_publish_and_exact_subscribe(self):
        received_events = []

        def on_order_created(envelope: EventEnvelope):
            received_events.append(envelope)

        self.bus.subscribe("order.created", on_order_created)
        envelope = self.bus.publish(
            event_type="order.created",
            data={"order_id": "12345"},
            level=EventLevel.INFO,
            tags=["order"],
        )

        self.assertEqual(len(received_events), 1)
        self.assertEqual(received_events[0].header.event_id, envelope.header.event_id)
        self.assertEqual(received_events[0].payload.data["order_id"], "12345")

        stats = self.bus.get_stats()
        self.assertEqual(stats["published_count"], 1)
        self.assertEqual(stats["consumed_count"], 1)
        self.assertEqual(stats["failed_count"], 0)

    def test_wildcard_subscribe(self):
        agent_events = []

        def on_agent_wildcard(envelope: EventEnvelope):
            agent_events.append(envelope.payload.event_type)

        self.bus.subscribe("agent.*", on_agent_wildcard)

        self.bus.publish("agent.started", {"step": 1})
        self.bus.publish("agent.thought", {"thought": "thinking"})
        self.bus.publish("tool.executed", {"tool": "calculator"})  # 不匹配

        self.assertEqual(agent_events, ["agent.started", "agent.thought"])
        stats = self.bus.get_stats()
        self.assertEqual(stats["published_count"], 3)
        self.assertEqual(stats["consumed_count"], 2)

    def test_unsubscribe(self):
        calls = []

        def handler(envelope: EventEnvelope):
            calls.append(envelope)

        self.bus.subscribe("test.event", handler)
        self.bus.publish("test.event", {"v": 1})
        self.assertEqual(len(calls), 1)

        self.bus.unsubscribe("test.event", handler)
        self.bus.publish("test.event", {"v": 2})
        self.assertEqual(len(calls), 1)  # 不再触发

        # 重复取消订阅不报错
        self.bus.unsubscribe("test.event", handler)

    def test_idempotent_duplicate_skip(self):
        calls = []

        def handler(envelope: EventEnvelope):
            calls.append(envelope)

        self.bus.subscribe("repeat.event", handler)

        envelope = self.bus.publish("repeat.event", {"attempt": 1})
        self.assertEqual(len(calls), 1)

        # 手动重复分发相同 event_id 的 envelope
        self.bus._dispatch(envelope)
        self.assertEqual(len(calls), 1)  # 幂等跳过，计数不增加

        # 清除缓存后允许再次分发
        self.bus.clear_processed_cache()
        self.bus._dispatch(envelope)
        self.assertEqual(len(calls), 2)

    def test_lifecycle_hooks_and_error_handling(self):
        bus = CustomAuditedEventBus()

        def failing_handler(envelope: EventEnvelope):
            raise RuntimeError("Database connection timeout")

        bus.subscribe("task.failed", failing_handler)
        envelope = bus.publish("task.failed", {"task_id": "T-99"})

        # 验证钩子执行
        self.assertEqual(len(bus.before_published_events), 1)
        self.assertEqual(len(bus.after_published_events), 1)
        self.assertEqual(bus.before_published_events[0].header.event_id, envelope.header.event_id)

        # 验证错误回调捕获
        self.assertEqual(len(bus.captured_errors), 1)
        handler_ref, err_envelope, err = bus.captured_errors[0]
        self.assertEqual(handler_ref, failing_handler)
        self.assertEqual(err_envelope.header.event_id, envelope.header.event_id)
        self.assertIsInstance(err, RuntimeError)

        stats = bus.get_stats()
        self.assertEqual(stats["published_count"], 1)
        self.assertEqual(stats["consumed_count"], 0)
        self.assertEqual(stats["failed_count"], 1)

    def test_cache_pruning(self):
        # 验证超过 10000 时自动修剪
        self.bus._processed_ids = {f"id_{i}" for i in range(10001)}
        dummy_header = EventHeader(event_id="new_id")
        dummy_envelope = EventEnvelope(
            header=dummy_header,
            payload=EventPayload(event_type="test"),
            context=EventContext(),
        )
        self.bus._mark_processed(dummy_envelope)
        # 修剪了约 5000 条，集合大小应在约 5002 左右
        self.assertLess(len(self.bus._processed_ids), 6000)
        self.assertIn("new_id", self.bus._processed_ids)
