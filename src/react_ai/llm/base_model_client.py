import logging
import threading
import time
import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

logger = logging.getLogger(__name__)


class ModelProvider(Enum):
    """模型供应商标识枚举。"""
    OPENAI = "openai"              # OpenAI 系列模型
    ANTHROPIC = "anthropic"        # Anthropic Claude 系列
    AZURE_OPENAI = "azure_openai"  # Azure OpenAI 服务
    QWEN = "qwen"                  # 阿里通义千问
    DEEPSEEK = "deepseek"          # DeepSeek 模型
    OLLAMA = "ollama"              # 本地 Ollama 部署
    CUSTOM = "custom"              # 自定义/私有模型


class ChatRole(Enum):
    """对话消息角色枚举。"""
    SYSTEM = "system"      # 系统提示，定义 Agent 人设或规则
    USER = "user"          # 用户输入
    ASSISTANT = "assistant"  # 模型回复
    TOOL = "tool"          # 工具调用结果


@dataclass
class ChatMessage:
    """单条对话消息，统一各模型的输入格式。"""
    role: ChatRole  # 消息角色
    content: str  # 消息文本内容
    name: str | None = None  # 可选的消息名称，用于工具调用标识
    tool_call_id: str | None = None  # 关联的工具调用ID，当 role=TOOL 时填写
    tool_calls: list[dict[str, Any]] | None = None  # 工具调用列表，当 role=ASSISTANT 时可能携带


@dataclass
class ModelUsage:
    """模型调用用量统计。"""
    prompt_tokens: int = 0  # 输入 Token 数量
    completion_tokens: int = 0  # 输出 Token 数量
    total_tokens: int = 0  # 总 Token 数量
    cost_usd: float = 0.0  # 本次调用估算成本（美元）


@dataclass
class ModelResponse:
    """模型调用响应结果。"""
    content: str  # 模型回复的文本内容
    model: str  # 实际使用的模型名称
    finish_reason: str = ""  # 结束原因，如 "stop"、"length"、"tool_calls"
    usage: ModelUsage | None = None  # Token 用量信息
    raw_response: dict[str, Any] | None = None  # 原始响应，用于调试
    trace_id: str | None = None  # 链路追踪ID
    latency_ms: int = 0  # 调用耗时（毫秒）


@dataclass
class ModelConfig:
    """模型调用配置，统一各模型的超参数。"""
    model: str = "gpt-4o-mini"  # 模型名称
    temperature: float = 0.7  # 温度参数，控制输出随机性（0-1）
    max_tokens: int | None = None  # 最大输出 Token 数，None 表示使用模型默认值
    top_p: float = 1.0  # 核采样参数
    stop: list[str] | None = None  # 停止词序列
    presence_penalty: float = 0.0  # 存在惩罚，降低重复话题
    frequency_penalty: float = 0.0  # 频率惩罚，降低重复用词
    timeout_seconds: float = 60.0  # 单次调用超时时间（秒）
    max_retries: int = 3  # 最大重试次数
    retry_base_delay: float = 1.0  # 重试基础延迟（秒），用于指数退避
    stream: bool = False  # 是否使用流式输出
    extra_body: dict[str, Any] = field(default_factory=dict)  # 额外参数，用于模型特有字段


class RetryableError(Exception):
    """可重试错误：网络抖动、限流、临时服务端错误。"""


class NonRetryableError(Exception):
    """不可重试错误：参数错误、鉴权失败、模型不存在。"""


class BaseModelClient(ABC):
    """模型客户端基类：统一 LLM 调用、重试、降级、用量统计和链路追踪。

    设计目标：
    - 屏蔽不同模型供应商的 API 差异，上层 Agent 只依赖统一接口；
    - 内置指数退避重试，区分可重试/不可重试错误；
    - 支持主备模型降级，提高可用性；
    - 记录 Token 用量和耗时，便于成本监控；
    - 输出结构化日志，串联 trace_id 便于全链路排查。
    """

    provider: ModelProvider = ModelProvider.CUSTOM  # 供应商标识
    default_model: str = ""  # 默认模型名称
    name: str = "base_model_client"  # 客户端实例名称，用于日志区分

    def __init__(
        self,
        api_key: str | None = None,
        base_url: str | None = None,
        fallback_client: "BaseModelClient | None" = None,
    ):
        self._api_key = api_key  # API 鉴权密钥
        self._base_url = base_url  # API 基础地址
        self._fallback = fallback_client  # 备用客户端，主客户端失败时自动降级
        self._lock = threading.RLock()  # 线程锁，保护共享状态
        self._total_calls: int = 0  # 累计调用次数
        self._total_failures: int = 0  # 累计失败次数
        self._total_tokens: int = 0  # 累计消耗 Token 数

    @property
    def total_calls(self) -> int:
        """获取累计调用次数。"""
        return self._total_calls

    @property
    def total_failures(self) -> int:
        """获取累计失败次数。"""
        return self._total_failures

    @property
    def total_tokens(self) -> int:
        """获取累计 Token 消耗。"""
        return self._total_tokens

    def chat(
        self,
        messages: list[ChatMessage],
        config: ModelConfig | None = None,
        trace_id: str | None = None,
    ) -> ModelResponse:
        """统一聊天入口：参数校验 → 重试调用 → 降级兜底 → 统计上报。"""
        trace_id = trace_id or uuid.uuid4().hex  # 生成或使用已有链路ID
        config = config or ModelConfig()  # 使用传入配置或默认配置

        # 参数校验
        if not messages:
            raise NonRetryableError("messages 不能为空")

        logger.info(
            "llm_call_start name=%s model=%s trace=%s msg_count=%d",
            self.name, config.model, trace_id, len(messages),
        )

        last_error: Exception | None = None

        # 重试循环：指数退避
        for attempt in range(1, config.max_retries + 1):
            try:
                response = self._do_chat_with_timeout(messages, config, trace_id)
                self._record_success(response)  # 记录成功统计
                logger.info(
                    "llm_call_ok name=%s model=%s trace=%s attempt=%d latency_ms=%d tokens=%d",
                    self.name, response.model, trace_id, attempt,
                    response.latency_ms, response.usage.total_tokens if response.usage else 0,
                )
                return response

            except NonRetryableError as exc:
                # 不可重试错误直接抛出，不浪费重试次数
                logger.warning(
                    "llm_call_non_retryable name=%s trace=%s error=%s",
                    self.name, trace_id, str(exc),
                )
                last_error = exc
                break  # 跳出重试循环

            except RetryableError as exc:
                last_error = exc
                if attempt < config.max_retries:
                    # 指数退避延迟：base * 2^(attempt-1)
                    delay = config.retry_base_delay * (2 ** (attempt - 1))
                    logger.warning(
                        "llm_call_retry name=%s trace=%s attempt=%d/%d delay=%.1fs error=%s",
                        self.name, trace_id, attempt, config.max_retries, delay, str(exc),
                    )
                    time.sleep(delay)  # 等待后重试
                else:
                    logger.error(
                        "llm_call_retries_exhausted name=%s trace=%s attempts=%d error=%s",
                        self.name, trace_id, attempt, str(exc),
                    )

            except Exception as exc:
                # 未分类异常视为可重试
                last_error = RetryableError(f"未知错误: {exc}")
                if attempt < config.max_retries:
                    delay = config.retry_base_delay * (2 ** (attempt - 1))
                    logger.warning(
                        "llm_call_retry_unknown name=%s trace=%s attempt=%d/%d delay=%.1fs error=%s",
                        self.name, trace_id, attempt, config.max_retries, delay, str(exc),
                    )
                    time.sleep(delay)
                else:
                    logger.exception(
                        "llm_call_failed name=%s trace=%s attempts=%d",
                        self.name, trace_id, attempt,
                    )

        # 所有重试耗尽，尝试降级到备用客户端
        if self._fallback:
            logger.info(
                "llm_fallback_start name=%s fallback=%s trace=%s",
                self.name, self._fallback.name, trace_id,
            )
            try:
                response = self._fallback.chat(messages, config, trace_id)
                response.content = f"[降级模型 {self._fallback.name}] {response.content}"
                return response
            except Exception as fallback_exc:  # noqa: BLE001
                logger.error(
                    "llm_fallback_failed name=%s fallback=%s trace=%s error=%s",
                    self.name, self._fallback.name, trace_id, str(fallback_exc),
                )

        # 最终失败
        self._record_failure()
        raise NonRetryableError(
            f"模型调用最终失败: name={self.name}, trace={trace_id}, error={last_error}"
        )

    def _do_chat_with_timeout(
        self,
        messages: list[ChatMessage],
        config: ModelConfig,
        trace_id: str,
    ) -> ModelResponse:
        """带超时保护的底层调用模板方法。"""
        start = time.time()  # 记录开始时间
        try:
            response = self._do_chat(messages, config, trace_id)  # 调用子类实现
            response.latency_ms = int((time.time() - start) * 1000)  # 计算耗时
            response.trace_id = trace_id  # 回填链路ID
            return response
        except Exception as exc:
            elapsed = int((time.time() - start) * 1000)
            logger.warning(
                "llm_call_error name=%s trace=%s latency_ms=%d error=%s",
                self.name, trace_id, elapsed, str(exc),
            )
            raise

    @abstractmethod
    def _do_chat(
        self,
        messages: list[ChatMessage],
        config: ModelConfig,
        trace_id: str,
    ) -> ModelResponse:
        """子类必须实现的底层调用逻辑。
        
        负责与具体模型供应商的 API 交互，解析响应并返回 ModelResponse。
        抛出 RetryableError 或 NonRetryableError 以指导重试策略。
        """

    def _record_success(self, response: ModelResponse):
        """记录成功调用统计。"""
        with self._lock:
            self._total_calls += 1  # 调用次数加一
            if response.usage:
                self._total_tokens += response.usage.total_tokens  # 累加 Token

    def _record_failure(self):
        """记录失败统计。"""
        with self._lock:
            self._total_calls += 1  # 调用次数加一
            self._total_failures += 1  # 失败次数加一

    def get_stats(self) -> dict[str, Any]:
        """获取客户端统计信息，用于监控面板。"""
        with self._lock:
            failure_rate = (
                self._total_failures / self._total_calls
                if self._total_calls > 0 else 0.0
            )
            return {
                "name": self.name,  # 客户端名称
                "provider": self.provider.value,  # 供应商标识
                "default_model": self.default_model,  # 默认模型
                "total_calls": self._total_calls,  # 累计调用
                "total_failures": self._total_failures,  # 累计失败
                "total_tokens": self._total_tokens,  # 累计 Token
                "failure_rate": round(failure_rate, 4),  # 失败率
                "has_fallback": self._fallback is not None,  # 是否有备用模型
            }

    @staticmethod
    def build_messages(
        system_prompt: str | None = None,
        user_prompt: str | None = None,
        history: list[ChatMessage] | None = None,
    ) -> list[ChatMessage]:
        """便捷方法：快速构建消息列表。"""
        messages: list[ChatMessage] = []
        if system_prompt:
            messages.append(ChatMessage(role=ChatRole.SYSTEM, content=system_prompt))
        if history:
            messages.extend(history)
        if user_prompt:
            messages.append(ChatMessage(role=ChatRole.USER, content=user_prompt))
        return messages

    @staticmethod
    def messages_to_dict(messages: list[ChatMessage]) -> list[dict[str, Any]]:
        """将消息列表转为字典列表，便于序列化或调试。"""
        result = []
        for msg in messages:
            item: dict[str, Any] = {"role": msg.role.value, "content": msg.content}
            if msg.name:
                item["name"] = msg.name
            if msg.tool_call_id:
                item["tool_call_id"] = msg.tool_call_id
            if msg.tool_calls:
                item["tool_calls"] = msg.tool_calls
            result.append(item)
        return result
