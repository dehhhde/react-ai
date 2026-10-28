"""
统一消息协议、消息信封与消息传输基类模块。

提供端到端结构化消息抽象：包含消息类型、优先级、生命周期状态、
头部路由元数据、载荷完整性校验（SHA256）、环境上下文隔离、
消息序列化与反序列化、发送/接收/分发、幂等去重、重试机制以及度量审计。
"""

import hashlib
import json
import logging
import threading
import time
import uuid
from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

logger = logging.getLogger(__name__)


class MessageType(Enum):
    """消息类型枚举，用于区分消息的用途。"""

    TEXT = "text"  # 纯文本消息
    IMAGE = "image"  # 图片消息
    FILE = "file"  # 文件消息
    AUDIO = "audio"  # 音频消息
    VIDEO = "video"  # 视频消息
    COMMAND = "command"  # 指令/控制消息，用于系统间协调
    EVENT = "event"  # 事件通知消息
    HEARTBEAT = "heartbeat"  # 心跳消息，用于保活和探活


class MessagePriority(Enum):
    """消息优先级枚举，用于调度消费顺序。"""

    LOW = 0  # 低优先级：日志、统计等非关键消息
    NORMAL = 1  # 普通优先级：常规业务消息
    HIGH = 2  # 高优先级：用户直接请求，需要快速响应
    URGENT = 3  # 紧急优先级：故障告警、熔断指令等


class MessageStatus(Enum):
    """消息状态枚举，用于跟踪消息生命周期。"""

    CREATED = "created"  # 已创建，尚未发送
    SENT = "sent"  # 已发送，等待确认
    DELIVERED = "delivered"  # 已送达，接收方已确认收到
    CONSUMED = "consumed"  # 已消费，业务逻辑已处理完成
    FAILED = "failed"  # 处理失败
    EXPIRED = "expired"  # 已过期，超过TTL未被消费
    DEAD_LETTER = "dead_letter"  # 死信，超过最大重试次数


@dataclass
class MessageHeader:
    """消息头，携带传输和路由所需的元数据。"""

    message_id: str = field(default_factory=lambda: uuid.uuid4().hex)  # 消息唯一ID，用于去重和追踪
    trace_id: str = ""  # 链路追踪ID，串联全链路日志
    span_id: str = ""  # 当前跨度ID，细化 trace 内的调用链
    parent_message_id: str = ""  # 父消息ID，用于构建消息因果链
    timestamp: float = field(default_factory=time.time)  # 消息创建时间戳
    ttl: float = 0.0  # 生存时间（秒），0 表示永不过期
    priority: MessagePriority = MessagePriority.NORMAL  # 消息优先级
    message_type: MessageType = MessageType.TEXT  # 消息类型
    correlation_id: str = ""  # 关联ID，用于请求-响应配对
    reply_to: str = ""  # 回复目标地址/队列名，用于响应路由
    retry_count: int = 0  # 当前重试次数
    max_retries: int = 3  # 最大重试次数
    source: str = ""  # 消息来源，如服务名、Agent 名称
    destination: str = ""  # 目标地址/队列名
    version: str = "1.0"  # 消息协议版本


@dataclass
class MessageBody:
    """消息体，承载实际业务内容。"""

    content: str = ""  # 消息主体内容，文本或序列化后的数据
    content_type: str = "text/plain"  # 内容类型，如 text/plain、application/json
    encoding: str = "utf-8"  # 内容编码
    attachments: list[dict[str, Any]] = field(default_factory=list)  # 附件列表，如文件引用、图片URL
    metadata: dict[str, Any] = field(default_factory=dict)  # 扩展元数据，如自定义标签
    schema_version: str = "1.0"  # 数据结构版本，用于兼容升级

    def compute_checksum(self) -> str:
        """计算消息体校验和，用于完整性验证。"""
        raw = json.dumps(
            {
                "content": self.content,
                "content_type": self.content_type,
                "attachments": self.attachments,
            },
            ensure_ascii=False,
            sort_keys=True,
        )  # 序列化关键内容
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()  # 返回 SHA256 哈希


@dataclass
class MessageContext:
    """消息上下文，携带环境和身份信息。"""

    tenant_id: str | None = None  # 租户ID，多租户隔离
    user_id: str | None = None  # 用户ID，标识发送者
    session_id: str | None = None  # 会话ID，关联对话或操作流程
    agent_id: str | None = None  # Agent ID，标识执行 Agent
    task_id: str | None = None  # 任务ID，标识当前任务
    environment: str = "production"  # 环境标识，如 dev / staging / production
    ip_address: str | None = None  # 发送者 IP 地址
    user_agent: str | None = None  # 发送者客户端信息
    extra: dict[str, Any] = field(default_factory=dict)  # 额外上下文，可扩展


@dataclass
class MessageEnvelope:
    """消息完整信封，包含头、体和上下文。"""

    header: MessageHeader  # 消息头
    body: MessageBody  # 消息体
    context: MessageContext  # 消息上下文
    status: MessageStatus = MessageStatus.CREATED  # 消息状态
    checksum: str = ""  # 消息体校验和，用于完整性验证
    serialized: str = ""  # 序列化后的字符串，用于存储或传输
    created_at: float = field(default_factory=time.time)  # 信封创建时间
    updated_at: float = field(default_factory=time.time)  # 最后更新时间

    def compute_checksum(self) -> str:
        """计算并缓存消息体校验和。"""
        self.checksum = self.body.compute_checksum()  # 委托给 body 计算
        return self.checksum

    def verify_checksum(self) -> bool:
        """验证消息体完整性。"""
        if not self.checksum:  # 没有校验和，无法验证
            return False
        return self.body.compute_checksum() == self.checksum  # 对比当前计算值与缓存值

    def is_expired(self) -> bool:
        """检查消息是否已过期。"""
        if self.header.ttl <= 0:  # TTL <= 0 表示永不过期
            return False
        elapsed = time.time() - self.header.timestamp  # 计算已流逝时间
        return elapsed > self.header.ttl  # 超过 TTL 则过期

    def serialize(self) -> str:
        """将消息序列化为 JSON 字符串。"""
        priority_val = (
            self.header.priority.value
            if isinstance(self.header.priority, MessagePriority)
            else self.header.priority
        )
        message_type_val = (
            self.header.message_type.value
            if isinstance(self.header.message_type, MessageType)
            else self.header.message_type
        )
        status_val = (
            self.status.value
            if isinstance(self.status, MessageStatus)
            else self.status
        )

        data = {
            "header": {
                "message_id": self.header.message_id,
                "trace_id": self.header.trace_id,
                "span_id": self.header.span_id,
                "parent_message_id": self.header.parent_message_id,
                "timestamp": self.header.timestamp,
                "ttl": self.header.ttl,
                "priority": priority_val,
                "message_type": message_type_val,
                "correlation_id": self.header.correlation_id,
                "reply_to": self.header.reply_to,
                "retry_count": self.header.retry_count,
                "max_retries": self.header.max_retries,
                "source": self.header.source,
                "destination": self.header.destination,
                "version": self.header.version,
            },
            "body": {
                "content": self.body.content,
                "content_type": self.body.content_type,
                "encoding": self.body.encoding,
                "attachments": self.body.attachments,
                "metadata": self.body.metadata,
                "schema_version": self.body.schema_version,
            },
            "context": {
                "tenant_id": self.context.tenant_id,
                "user_id": self.context.user_id,
                "session_id": self.context.session_id,
                "agent_id": self.context.agent_id,
                "task_id": self.context.task_id,
                "environment": self.context.environment,
                "ip_address": self.context.ip_address,
                "user_agent": self.context.user_agent,
                "extra": self.context.extra,
            },
            "status": status_val,
            "checksum": self.checksum,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }
        self.serialized = json.dumps(data, ensure_ascii=False, default=str)  # 序列化为 JSON
        return self.serialized

    @classmethod
    def deserialize(cls, data: str) -> "MessageEnvelope":
        """从 JSON 字符串反序列化消息。"""
        obj = json.loads(data)  # 解析 JSON

        raw_priority = obj["header"].get("priority", 1)
        try:
            priority = MessagePriority(raw_priority)
        except (ValueError, TypeError):
            priority = MessagePriority.NORMAL

        raw_type = obj["header"].get("message_type", "text")
        try:
            message_type = MessageType(raw_type)
        except (ValueError, TypeError):
            message_type = MessageType.TEXT

        raw_status = obj.get("status", "created")
        try:
            status = MessageStatus(raw_status)
        except (ValueError, TypeError):
            status = MessageStatus.CREATED

        header = MessageHeader(
            message_id=obj["header"]["message_id"],
            trace_id=obj["header"].get("trace_id", ""),
            span_id=obj["header"].get("span_id", ""),
            parent_message_id=obj["header"].get("parent_message_id", ""),
            timestamp=obj["header"].get("timestamp", time.time()),
            ttl=obj["header"].get("ttl", 0.0),
            priority=priority,
            message_type=message_type,
            correlation_id=obj["header"].get("correlation_id", ""),
            reply_to=obj["header"].get("reply_to", ""),
            retry_count=obj["header"].get("retry_count", 0),
            max_retries=obj["header"].get("max_retries", 3),
            source=obj["header"].get("source", ""),
            destination=obj["header"].get("destination", ""),
            version=obj["header"].get("version", "1.0"),
        )
        body = MessageBody(
            content=obj["body"].get("content", ""),
            content_type=obj["body"].get("content_type", "text/plain"),
            encoding=obj["body"].get("encoding", "utf-8"),
            attachments=obj["body"].get("attachments", []),
            metadata=obj["body"].get("metadata", {}),
            schema_version=obj["body"].get("schema_version", "1.0"),
        )
        ctx = obj.get("context", {})
        context = MessageContext(
            tenant_id=ctx.get("tenant_id"),
            user_id=ctx.get("user_id"),
            session_id=ctx.get("session_id"),
            agent_id=ctx.get("agent_id"),
            task_id=ctx.get("task_id"),
            environment=ctx.get("environment", "production"),
            ip_address=ctx.get("ip_address"),
            user_agent=ctx.get("user_agent"),
            extra=ctx.get("extra", {}),
        )
        envelope = cls(
            header=header,
            body=body,
            context=context,
            status=status,
            checksum=obj.get("checksum", ""),
            serialized=data,
            created_at=obj.get("created_at", time.time()),
            updated_at=obj.get("updated_at", time.time()),
        )
        return envelope


class BaseMessage(ABC):
    """消息基类：统一消息的构建、发送、接收、确认和重试。

    设计目标：
    - 为 Agent / 服务间通信提供结构化消息抽象；
    - 统一消息格式，包含链路追踪、租户隔离、优先级调度；
    - 支持同步/异步发送，带超时和重试机制；
    - 内置消息去重和完整性校验；
    - 输出结构化日志，便于消息审计和问题排查。
    """

    name: str = "base_message"  # 消息通道名称，用于日志区分
    default_ttl: float = 300.0  # 默认生存时间（秒）
    max_retry_delay: float = 60.0  # 最大重试延迟（秒）

    def __init__(
        self,
        name: str | None = None,
        default_ttl: float | None = None,
        max_retry_delay: float | None = None,
    ):
        if name:
            self.name = name
        if default_ttl is not None:
            self.default_ttl = default_ttl
        if max_retry_delay is not None:
            self.max_retry_delay = max_retry_delay

        self._lock = threading.RLock()  # 线程锁，保护共享状态
        self._handlers: dict[str, list[Callable[[MessageEnvelope], Any]]] = {}  # 消息处理器注册表：message_type -> [handler, ...]
        self._sent_count: int = 0  # 累计发送消息数
        self._received_count: int = 0  # 累计接收消息数
        self._consumed_count: int = 0  # 累计消费消息数
        self._failed_count: int = 0  # 累计处理失败数
        self._processed_ids: set[str] = set()  # 已处理消息ID集合，用于幂等
        self._pending: dict[str, MessageEnvelope] = {}  # 待确认消息：message_id -> envelope

    def register_handler(self, message_type: str, handler: Callable[[MessageEnvelope], Any]):
        """注册指定类型的消息处理器。

        Args:
            message_type: 消息类型，如 "text"、"command" 或通配符 "*"
            handler: 回调函数，接收 MessageEnvelope 参数
        """
        with self._lock:
            if message_type not in self._handlers:
                self._handlers[message_type] = []
            self._handlers[message_type].append(handler)
            logger.info(
                "message_handler_registered name=%s type=%s handlers=%d",
                self.name,
                message_type,
                len(self._handlers[message_type]),
            )

    def unregister_handler(self, message_type: str, handler: Callable[[MessageEnvelope], Any]):
        """注销指定类型的消息处理器。

        Args:
            message_type: 消息类型
            handler: 要移除的回调函数
        """
        with self._lock:
            if message_type in self._handlers:
                try:
                    self._handlers[message_type].remove(handler)
                    logger.info("message_handler_unregistered name=%s type=%s", self.name, message_type)
                except ValueError:
                    pass  # 处理器不在列表中，忽略

    def send(
        self,
        content: str,
        message_type: MessageType = MessageType.TEXT,
        content_type: str = "text/plain",
        context: MessageContext | None = None,
        priority: MessagePriority = MessagePriority.NORMAL,
        ttl: float | None = None,
        destination: str = "",
        correlation_id: str = "",
        reply_to: str = "",
        trace_id: str | None = None,
        parent_message_id: str | None = None,
        attachments: list[dict[str, Any]] | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> MessageEnvelope:
        """发送一条消息。

        Args:
            content: 消息内容
            message_type: 消息类型
            content_type: 内容类型
            context: 消息上下文
            priority: 消息优先级
            ttl: 生存时间（秒），None 则使用默认值
            destination: 目标地址/队列
            correlation_id: 关联ID，用于请求-响应配对
            reply_to: 回复目标
            trace_id: 链路追踪ID，不传则自动生成
            parent_message_id: 父消息ID
            attachments: 附件列表
            metadata: 扩展元数据

        Returns:
            构建好的 MessageEnvelope
        """
        context = context or MessageContext()  # 使用传入上下文或新建
        attachments = attachments or []  # 确保附件不为 None
        metadata = metadata or {}  # 确保元数据不为 None
        ttl_val = ttl if ttl is not None else self.default_ttl  # 使用传入 TTL 或默认值

        # 构建消息头
        header = MessageHeader(
            trace_id=trace_id or uuid.uuid4().hex,
            parent_message_id=parent_message_id or "",
            source=self.name,
            destination=destination,
            priority=priority,
            message_type=message_type,
            ttl=ttl_val,
            correlation_id=correlation_id,
            reply_to=reply_to,
        )

        # 构建消息体
        body = MessageBody(
            content=content,
            content_type=content_type,
            attachments=attachments,
            metadata=metadata,
        )

        # 构建信封
        envelope = MessageEnvelope(
            header=header,
            body=body,
            context=context,
            status=MessageStatus.CREATED,
        )

        return self.send_envelope(envelope)

    def send_envelope(self, envelope: MessageEnvelope) -> MessageEnvelope:
        """发送已构建好的消息信封。

        Args:
            envelope: 待发送的消息信封

        Returns:
            发送完成后的 MessageEnvelope
        """
        # 计算校验和
        envelope.compute_checksum()

        # 序列化
        envelope.serialize()

        # 发送前钩子
        self._before_send(envelope)

        # 标记为已发送
        envelope.status = MessageStatus.SENT
        envelope.updated_at = time.time()

        # 记录发送
        with self._lock:
            self._sent_count += 1
            self._pending[envelope.header.message_id] = envelope  # 加入待确认队列

        # 实际传输（子类实现）
        self._do_send(envelope)

        # 发送后钩子
        self._after_send(envelope)

        message_type_val = (
            envelope.header.message_type.value
            if isinstance(envelope.header.message_type, MessageType)
            else str(envelope.header.message_type)
        )
        priority_val = (
            envelope.header.priority.value
            if isinstance(envelope.header.priority, MessagePriority)
            else str(envelope.header.priority)
        )

        logger.info(
            "message_send name=%s type=%s id=%s trace=%s priority=%s ttl=%.1f dest=%s",
            self.name,
            message_type_val,
            envelope.header.message_id,
            envelope.header.trace_id,
            priority_val,
            envelope.header.ttl,
            envelope.header.destination,
        )

        return envelope

    def receive(self, data: str) -> MessageEnvelope:
        """接收并解析一条消息。

        Args:
            data: 序列化的消息字符串

        Returns:
            解析后的 MessageEnvelope
        """
        envelope = MessageEnvelope.deserialize(data)  # 反序列化
        return self.receive_envelope(envelope)

    def receive_envelope(self, envelope: MessageEnvelope) -> MessageEnvelope:
        """接收并分发反序列化后的信封对象。

        Args:
            envelope: 消息信封

        Returns:
            处理后的 MessageEnvelope
        """
        envelope.status = MessageStatus.DELIVERED  # 标记为已送达
        envelope.updated_at = time.time()

        # 验证校验和
        if not envelope.verify_checksum():
            logger.warning(
                "message_checksum_invalid id=%s source=%s",
                envelope.header.message_id,
                envelope.header.source,
            )
            envelope.status = MessageStatus.FAILED

        # 检查是否过期
        if envelope.is_expired():
            logger.warning(
                "message_expired id=%s ttl=%.1f age=%.1f",
                envelope.header.message_id,
                envelope.header.ttl,
                time.time() - envelope.header.timestamp,
            )
            envelope.status = MessageStatus.EXPIRED

        # 幂等检查
        if self._is_duplicate(envelope):
            message_type_val = (
                envelope.header.message_type.value
                if isinstance(envelope.header.message_type, MessageType)
                else str(envelope.header.message_type)
            )
            logger.info(
                "message_duplicate_skip id=%s type=%s",
                envelope.header.message_id,
                message_type_val,
            )
            return envelope

        # 记录接收
        with self._lock:
            self._received_count += 1

        # 分发到处理器
        self._dispatch(envelope)

        message_type_val = (
            envelope.header.message_type.value
            if isinstance(envelope.header.message_type, MessageType)
            else str(envelope.header.message_type)
        )
        status_val = (
            envelope.status.value
            if isinstance(envelope.status, MessageStatus)
            else str(envelope.status)
        )

        logger.info(
            "message_receive name=%s type=%s id=%s trace=%s status=%s",
            self.name,
            message_type_val,
            envelope.header.message_id,
            envelope.header.trace_id,
            status_val,
        )

        return envelope

    def _dispatch(self, envelope: MessageEnvelope):
        """将消息分发到匹配的处理器。

        Args:
            envelope: 消息信封
        """
        message_type = (
            envelope.header.message_type.value
            if isinstance(envelope.header.message_type, MessageType)
            else str(envelope.header.message_type)
        )
        handlers: list[Callable[[MessageEnvelope], Any]] = []

        with self._lock:
            # 查找精确匹配的处理器
            if message_type in self._handlers:
                handlers.extend(self._handlers[message_type])
            # 查找全局/通配符订阅
            if "*" in self._handlers:
                handlers.extend(self._handlers["*"])

        if not handlers:
            logger.debug("message_no_handler type=%s id=%s", message_type, envelope.header.message_id)
            envelope.status = MessageStatus.CONSUMED  # 无处理器，直接标记为已消费
            self._mark_processed(envelope)
            return

        # 逐个调用处理器
        for handler in handlers:
            self._invoke_handler(handler, envelope)

    def _invoke_handler(self, handler: Callable[[MessageEnvelope], Any], envelope: MessageEnvelope):
        """调用单个消息处理器，带异常捕获。

        Args:
            handler: 回调函数
            envelope: 消息信封
        """
        message_type_val = (
            envelope.header.message_type.value
            if isinstance(envelope.header.message_type, MessageType)
            else str(envelope.header.message_type)
        )
        try:
            handler(envelope)  # 调用处理器
            self._mark_processed(envelope)  # 标记为已处理
            with self._lock:
                self._consumed_count += 1
            envelope.status = MessageStatus.CONSUMED
            envelope.updated_at = time.time()
            logger.debug(
                "message_consumed handler=%s type=%s id=%s",
                handler.__name__ if hasattr(handler, "__name__") else str(handler),
                message_type_val,
                envelope.header.message_id,
            )
        except Exception as exc:  # noqa: BLE001
            with self._lock:
                self._failed_count += 1
            envelope.status = MessageStatus.FAILED
            envelope.updated_at = time.time()
            logger.error(
                "message_handler_error handler=%s type=%s id=%s error=%s",
                handler.__name__ if hasattr(handler, "__name__") else str(handler),
                message_type_val,
                envelope.header.message_id,
                str(exc),
            )
            self._on_handler_error(handler, envelope, exc)

    def acknowledge(self, message_id: str) -> bool:
        """确认消息已处理，从待确认队列中移除。

        Args:
            message_id: 消息ID

        Returns:
            是否成功确认
        """
        with self._lock:
            if message_id in self._pending:
                envelope = self._pending.pop(message_id)  # 从待确认队列移除
                envelope.status = MessageStatus.CONSUMED
                envelope.updated_at = time.time()
                logger.debug("message_acknowledged id=%s", message_id)
                return True
            return False

    def _is_duplicate(self, envelope: MessageEnvelope) -> bool:
        """检查消息是否已处理过（幂等）。

        Args:
            envelope: 消息信封

        Returns:
            是否重复
        """
        with self._lock:
            return envelope.header.message_id in self._processed_ids

    def _mark_processed(self, envelope: MessageEnvelope):
        """标记消息为已处理。

        Args:
            envelope: 消息信封
        """
        with self._lock:
            # 防止集合无限增长，达到上限时清理一半
            if len(self._processed_ids) >= 10000:
                to_remove = list(self._processed_ids)[:5000]
                for mid in to_remove:
                    self._processed_ids.discard(mid)
            self._processed_ids.add(envelope.header.message_id)

    def _before_send(self, envelope: MessageEnvelope):
        """发送前钩子，子类可覆盖实现校验、加密或增强。

        Args:
            envelope: 消息信封
        """

    def _after_send(self, envelope: MessageEnvelope):
        """发送后钩子，子类可覆盖实现异步持久化或审计上报。

        Args:
            envelope: 消息信封
        """

    @abstractmethod
    def _do_send(self, envelope: MessageEnvelope):
        """实际消息传输执行（抽象方法，由子类实现）。

        Args:
            envelope: 待发送的消息信封
        """

    def _on_handler_error(
        self,
        handler: Callable[[MessageEnvelope], Any],
        envelope: MessageEnvelope,
        error: Exception,
    ):
        """消息处理器异常回调，子类可覆盖实现重试、告警或死信路由。

        Args:
            handler: 出错的处理器
            envelope: 消息信封
            error: 捕获的异常对象
        """

    def retry(self, message_id: str) -> bool:
        """重试发送未确认或处理失败的消息。

        Args:
            message_id: 消息ID

        Returns:
            是否触发重试成功
        """
        with self._lock:
            if message_id not in self._pending:
                logger.warning("message_retry_not_found id=%s", message_id)
                return False
            envelope = self._pending[message_id]
            if envelope.header.retry_count >= envelope.header.max_retries:
                envelope.status = MessageStatus.DEAD_LETTER
                envelope.updated_at = time.time()
                logger.warning(
                    "message_retry_exceeded_max id=%s max_retries=%d",
                    message_id,
                    envelope.header.max_retries,
                )
                return False
            envelope.header.retry_count += 1
            envelope.status = MessageStatus.SENT
            envelope.updated_at = time.time()

        try:
            self._before_send(envelope)
            self._do_send(envelope)
            self._after_send(envelope)
            logger.info(
                "message_retried name=%s id=%s retry_count=%d",
                self.name,
                message_id,
                envelope.header.retry_count,
            )
            return True
        except Exception as exc:  # noqa: BLE001
            with self._lock:
                self._failed_count += 1
                envelope.status = MessageStatus.FAILED
                envelope.updated_at = time.time()
            logger.error("message_retry_failed id=%s error=%s", message_id, str(exc))
            return False

    def get_stats(self) -> dict[str, Any]:
        """获取消息通道/总线统计信息，用于度量与监控。"""
        with self._lock:
            handler_count = sum(len(h) for h in self._handlers.values())
            return {
                "name": self.name,
                "sent_count": self._sent_count,
                "received_count": self._received_count,
                "consumed_count": self._consumed_count,
                "failed_count": self._failed_count,
                "pending_count": len(self._pending),
                "handler_count": handler_count,
                "processed_id_cache_size": len(self._processed_ids),
                "message_types": list(self._handlers.keys()),
            }

    def clear_processed_cache(self):
        """清空已处理消息ID缓存，释放内存。"""
        with self._lock:
            self._processed_ids.clear()
            logger.info("message_cache_cleared name=%s", self.name)


class InMemoryMessageBus(BaseMessage):
    """基于内存的消息总线实现，提供单进程/多组件间同步与环回分发。"""

    name: str = "in_memory_message_bus"

    def __init__(
        self,
        name: str | None = None,
        auto_dispatch: bool = True,
        default_ttl: float | None = None,
        max_retry_delay: float | None = None,
    ):
        super().__init__(name=name, default_ttl=default_ttl, max_retry_delay=max_retry_delay)
        self.auto_dispatch = auto_dispatch
        self.outbox: list[MessageEnvelope] = []

    def _do_send(self, envelope: MessageEnvelope):
        """将消息存入发件箱，若开启 auto_dispatch 则模拟网络接收并本地投递。"""
        self.outbox.append(envelope)
        if self.auto_dispatch:
            self.receive(envelope.serialized)

