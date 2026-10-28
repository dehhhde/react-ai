"""统一提示词基类、消息协议与渲染契约."""
import logging
import time
import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class PromptMessage:
    role: str  # system / user / assistant
    content: str

    def to_chat_message(self) -> Any:
        """转换为 react-ai 统一协议的 ChatMessage 模型."""
        from react_ai.schemas.messages import ChatMessage, MessageRole

        return ChatMessage(role=MessageRole(self.role), content=self.content)


@dataclass
class PromptResult:
    messages: list[PromptMessage]  # 渲染后的完整消息列表
    prompt_name: str               # 使用的 Prompt 名称
    version: str                   # 使用的版本
    trace_id: str                  # 链路追踪ID
    variables_used: dict[str, Any] # 实际注入的变量快照，用于审计
    render_duration_ms: int        # 渲染耗时

    def to_chat_messages(self) -> list[Any]:
        """将渲染结果中的所有消息转换为 ChatMessage 列表."""
        return [msg.to_chat_message() for msg in self.messages]


class BasePrompt(ABC):
    """Prompt 基类：统一模板、变量校验、渲染、输出约束和审计。"""

    name: str = ""                     # Prompt 唯一标识，如 "after_sales_router"
    version: str = "1.0.0"             # 版本号，便于 A/B 测试和回滚
    description: str = ""              # 用途描述
    tags: list[str] = field(default_factory=list)  # 标签，如 ["router", "after_sales"]

    # 输出约束：告诉模型应该以什么格式返回
    output_format: str = ""            # 例如 "json"、"structured"、"free_text"
    output_schema: dict | None = None  # JSON Schema，用于结构化输出校验

    # 变量声明
    required_variables: list[str] = field(default_factory=list)   # 必填变量，如 ["order_id", "user_question"]
    optional_variables: list[str] = field(default_factory=list)   # 可选变量，如 ["chat_history", "policy_docs"]

    def __init__(self):
        if not isinstance(getattr(self, "tags", None), list):
            self.tags = []
        if not isinstance(getattr(self, "required_variables", None), list):
            self.required_variables = []
        if not isinstance(getattr(self, "optional_variables", None), list):
            self.optional_variables = []

    @property
    @abstractmethod
    def system_template(self) -> str:
        """系统提示模板，定义角色、职责、边界、输出格式。"""

    @property
    def user_template(self) -> str:
        """用户提示模板，通常是当前轮次的用户输入 + 上下文。"""
        return "{{user_input}}"

    @property
    def examples(self) -> list[dict]:
        """少样本示例，格式为 [{"input": ..., "output": ...}, ...]"""
        return []

    def validate_variables(self, variables: dict) -> list[str]:
        """校验变量是否齐全，返回缺失的变量名列表。"""
        missing = []
        req_vars = self.required_variables if isinstance(self.required_variables, (list, tuple, set)) else []
        for var in req_vars:
            if var not in variables or variables[var] is None:
                missing.append(var)
        return missing

    def _render_template(self, template: str, variables: dict) -> str:
        """简单模板渲染：用 {{key}} 占位符替换为变量值。"""
        result = template
        for key, value in variables.items():
            placeholder = "{{" + key + "}}"
            if placeholder in result:
                result = result.replace(placeholder, str(value))
        return result

    def render(self, variables: dict, trace_id: str | None = None) -> PromptResult:
        """渲染 Prompt，返回完整的消息列表。"""
        trace_id = trace_id or uuid.uuid4().hex
        start = time.time()

        # 1. 变量校验
        missing = self.validate_variables(variables)
        if missing:
            raise ValueError(
                f"Prompt '{self.name}' 缺少必填变量: {missing}"
            )

        # 2. 渲染系统提示
        system_content = self._render_template(self.system_template, variables)

        # 3. 构建消息列表
        messages: list[PromptMessage] = []
        messages.append(PromptMessage(role="system", content=system_content))

        # 4. 注入少样本示例
        for example in self.examples:
            messages.append(PromptMessage(role="user", content=str(example.get("input", ""))))
            messages.append(PromptMessage(role="assistant", content=str(example.get("output", ""))))

        # 5. 渲染用户提示
        user_content = self._render_template(self.user_template, variables)
        messages.append(PromptMessage(role="user", content=user_content))

        # 6. 审计日志
        duration = int((time.time() - start) * 1000)
        logger.info(
            "prompt_rendered name=%s version=%s trace=%s duration_ms=%d vars=%s",
            self.name, self.version, trace_id, duration, list(variables.keys()),
        )

        return PromptResult(
            messages=messages,
            prompt_name=self.name,
            version=self.version,
            trace_id=trace_id,
            variables_used=variables,
            render_duration_ms=duration,
        )
