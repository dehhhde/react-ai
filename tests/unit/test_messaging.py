"""
Unit tests for react_ai.messaging module.
"""

import time

from react_ai.messaging import (
    BaseMessage,
    InMemoryMessageBus,
    MessageBody,
    MessageContext,
    MessageEnvelope,
    MessageHeader,
    MessagePriority,
    MessageStatus,
    MessageType,
)


class TestMessageDataStructures:
    """测试消息枚举、头、体、上下文与信封数据结构。"""

    def test_enums(self):
        assert MessageType.TEXT.value == "text"
        assert MessageType.COMMAND.value == "command"
        assert MessageType.EVENT.value == "event"
        assert MessageType.HEARTBEAT.value == "heartbeat"

        assert MessagePriority.LOW.value == 0
        assert MessagePriority.NORMAL.value == 1
        assert MessagePriority.HIGH.value == 2
        assert MessagePriority.URGENT.value == 3

        assert MessageStatus.CREATED.value == "created"
        assert MessageStatus.SENT.value == "sent"
        assert MessageStatus.DELIVERED.value == "delivered"
        assert MessageStatus.CONSUMED.value == "consumed"
        assert MessageStatus.FAILED.value == "failed"
        assert MessageStatus.EXPIRED.value == "expired"
        assert MessageStatus.DEAD_LETTER.value == "dead_letter"

    def test_header_defaults(self):
        header = MessageHeader()
        assert len(header.message_id) == 32
        assert header.priority == MessagePriority.NORMAL
        assert header.message_type == MessageType.TEXT
        assert header.max_retries == 3
        assert header.retry_count == 0
        assert header.timestamp > 0

    def test_body_checksum(self):
        body = MessageBody(
            content="test content",
            content_type="text/plain",
            attachments=[{"name": "file.txt", "size": 128}],
        )
        checksum1 = body.compute_checksum()
        checksum2 = body.compute_checksum()
        assert checksum1 == checksum2
        assert len(checksum1) == 64

        # 修改内容校验和改变
        body.content = "modified content"
        assert body.compute_checksum() != checksum1

    def test_envelope_checksum_verification(self):
        header = MessageHeader(message_type=MessageType.TEXT)
        body = MessageBody(content="verify test")
        context = MessageContext(tenant_id="tenant-1")
        envelope = MessageEnvelope(header=header, body=body, context=context)

        # 初始未计算校验和
        assert not envelope.verify_checksum()

        # 计算并校验
        calc_checksum = envelope.compute_checksum()
        assert envelope.checksum == calc_checksum
        assert envelope.verify_checksum()

        # 篡改 body 内容导致校验失败
        body.content = "tampered content"
        assert not envelope.verify_checksum()

    def test_envelope_expiration(self):
        header = MessageHeader(ttl=0.0)
        envelope = MessageEnvelope(
            header=header,
            body=MessageBody(),
            context=MessageContext(),
        )
        assert not envelope.is_expired()

        # 设置 10 秒 TTL，刚创建未过期
        header.ttl = 10.0
        assert not envelope.is_expired()

        # 模拟过去创建的消息已过期
        header.timestamp = time.time() - 20.0
        assert envelope.is_expired()

    def test_serialization_and_deserialization_roundtrip(self):
        header = MessageHeader(
            trace_id="trace-123",
            span_id="span-456",
            source="test_agent",
            destination="audit_queue",
            priority=MessagePriority.HIGH,
            message_type=MessageType.COMMAND,
            ttl=60.0,
            correlation_id="corr-789",
            reply_to="reply_queue",
        )
        body = MessageBody(
            content="run_diagnostics",
            content_type="application/json",
            metadata={"urgency": "high"},
            attachments=[{"id": "att-1"}],
        )
        context = MessageContext(
            tenant_id="tenant-alpha",
            user_id="user-99",
            session_id="session-001",
            agent_id="agent-007",
            environment="staging",
            extra={"client": "cli"},
        )
        envelope = MessageEnvelope(
            header=header,
            body=body,
            context=context,
            status=MessageStatus.SENT,
        )
        envelope.compute_checksum()

        # 序列化
        serialized = envelope.serialize()
        assert isinstance(serialized, str)

        # 反序列化
        restored = MessageEnvelope.deserialize(serialized)
        assert restored.header.message_id == envelope.header.message_id
        assert restored.header.trace_id == "trace-123"
        assert restored.header.priority == MessagePriority.HIGH
        assert restored.header.message_type == MessageType.COMMAND
        assert restored.header.ttl == 60.0
        assert restored.body.content == "run_diagnostics"
        assert restored.body.metadata == {"urgency": "high"}
        assert restored.context.tenant_id == "tenant-alpha"
        assert restored.context.extra == {"client": "cli"}
        assert restored.status == MessageStatus.SENT
        assert restored.verify_checksum()


class DummyMessageBus(BaseMessage):
    """测试用简单消息总线实现。"""

    name = "dummy_bus"

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.sent_envelopes: list[MessageEnvelope] = []

    def _do_send(self, envelope: MessageEnvelope):
        self.sent_envelopes.append(envelope)


class TestBaseMessageOperations:
    """测试 BaseMessage 发送、接收、分发、幂等、重试与统计。"""

    def test_send_and_receive_flow(self):
        bus = DummyMessageBus()
        received_messages: list[MessageEnvelope] = []

        def text_handler(env: MessageEnvelope):
            received_messages.append(env)

        bus.register_handler("text", text_handler)

        # 发送消息
        sent_env = bus.send(
            content="Hello Agent",
            message_type=MessageType.TEXT,
            trace_id="trace-abc",
            destination="agent_b",
        )

        assert sent_env.status == MessageStatus.SENT
        assert len(bus.sent_envelopes) == 1
        assert bus.sent_envelopes[0].header.message_id == sent_env.header.message_id

        # 接收并消费
        delivered_env = bus.receive(sent_env.serialized)
        assert delivered_env.status == MessageStatus.CONSUMED
        assert len(received_messages) == 1
        assert received_messages[0].body.content == "Hello Agent"

    def test_wildcard_handler_dispatch(self):
        bus = DummyMessageBus()
        text_received = []
        wildcard_received = []

        bus.register_handler("text", lambda env: text_received.append(env))
        bus.register_handler("*", lambda env: wildcard_received.append(env))

        env = bus.send(content="test wildcard", message_type=MessageType.TEXT)
        bus.receive(env.serialized)

        assert len(text_received) == 1
        assert len(wildcard_received) == 1

    def test_no_handler_auto_consumed(self):
        bus = DummyMessageBus()
        env = bus.send(content="no handler", message_type=MessageType.HEARTBEAT)
        result = bus.receive(env.serialized)
        assert result.status == MessageStatus.CONSUMED

    def test_handler_error_and_failure_counter(self):
        bus = DummyMessageBus()
        error_records = []

        def faulty_handler(env: MessageEnvelope):
            raise ValueError("Processing exploded")

        def on_error(handler, env, exc):
            error_records.append((env.header.message_id, str(exc)))

        bus._on_handler_error = on_error
        bus.register_handler("command", faulty_handler)

        env = bus.send(content="bad_command", message_type=MessageType.COMMAND)
        result = bus.receive(env.serialized)

        assert result.status == MessageStatus.FAILED
        assert len(error_records) == 1
        assert error_records[0][0] == env.header.message_id
        assert "Processing exploded" in error_records[0][1]

        stats = bus.get_stats()
        assert stats["failed_count"] == 1

    def test_acknowledgment(self):
        bus = DummyMessageBus()
        env = bus.send(content="ack test")
        msg_id = env.header.message_id

        stats = bus.get_stats()
        assert stats["pending_count"] == 1

        # 成功确认
        assert bus.acknowledge(msg_id) is True
        assert bus.get_stats()["pending_count"] == 0

        # 重复确认返回 False
        assert bus.acknowledge(msg_id) is False

    def test_idempotent_deduplication(self):
        bus = DummyMessageBus()
        count = [0]

        bus.register_handler("text", lambda env: count.__setitem__(0, count[0] + 1))

        env = bus.send(content="dedup test")

        # 首次处理
        bus.receive(env.serialized)
        assert count[0] == 1

        # 第二次接收相同消息被跳过
        bus.receive(env.serialized)
        assert count[0] == 1

        # 清除缓存后可再次处理
        bus.clear_processed_cache()
        assert bus.get_stats()["processed_id_cache_size"] == 0

    def test_retry_mechanism(self):
        bus = DummyMessageBus()
        env = bus.send(content="retryable task")
        msg_id = env.header.message_id

        assert env.header.retry_count == 0

        # 第一次重试
        assert bus.retry(msg_id) is True
        assert env.header.retry_count == 1
        assert env.status == MessageStatus.SENT

        # 模拟达到最大重试上限
        env.header.retry_count = 3
        env.header.max_retries = 3

        # 再次重试触发死信
        assert bus.retry(msg_id) is False
        assert env.status == MessageStatus.DEAD_LETTER

        # 未知消息重试返回 False
        assert bus.retry("non-existent-id") is False

    def test_unregister_handler(self):
        bus = DummyMessageBus()
        handler = lambda env: None
        bus.register_handler("text", handler)
        assert bus.get_stats()["handler_count"] == 1

        bus.unregister_handler("text", handler)
        assert bus.get_stats()["handler_count"] == 0

        # 注销不存在的处理器不抛出异常
        bus.unregister_handler("text", handler)


class TestInMemoryMessageBus:
    """测试开箱即用的 InMemoryMessageBus 实现。"""

    def test_auto_dispatch_enabled(self):
        bus = InMemoryMessageBus(name="local_bus", auto_dispatch=True)
        received = []

        bus.register_handler("text", lambda env: received.append(env.body.content))

        bus.send(content="instant delivery")

        assert len(bus.outbox) == 1
        assert len(received) == 1
        assert received[0] == "instant delivery"

    def test_auto_dispatch_disabled(self):
        bus = InMemoryMessageBus(auto_dispatch=False)
        received = []

        bus.register_handler("text", lambda env: received.append(env))

        bus.send(content="queued only")

        assert len(bus.outbox) == 1
        assert len(received) == 0  # 未自动投递
