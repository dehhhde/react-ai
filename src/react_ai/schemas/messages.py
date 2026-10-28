"""消息数据模型与协议定义."""
from enum import Enum
from typing import Any, Dict, List, Optional
from pydantic import BaseModel, Field
import time


class MessageRole(str, Enum):
    SYSTEM = "system"
    USER = "user"
    ASSISTANT = "assistant"
    TOOL = "tool"


class ToolCall(BaseModel):
    id: str = Field(..., description="工具调用唯一ID")
    name: str = Field(..., description="调用的工具函数名")
    arguments: Dict[str, Any] = Field(default_factory=dict, description="入参字典")


class ToolCallResult(BaseModel):
    tool_call_id: str = Field(..., description="对应的ToolCall ID")
    name: str = Field(..., description="调用的工具名")
    output: Any = Field(..., description="工具执行输出")
    is_error: bool = Field(default=False, description="是否发生异常")
    execution_time_ms: float = Field(default=0.0, description="执行耗时毫秒")


class UsageStats(BaseModel):
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    estimated_cost_usd: float = 0.0


class ChatMessage(BaseModel):
    role: MessageRole
    content: Optional[str] = None
    name: Optional[str] = None
    tool_calls: Optional[List[ToolCall]] = None
    tool_call_id: Optional[str] = None
    metadata: Dict[str, Any] = Field(default_factory=dict)
    timestamp: float = Field(default_factory=time.time)
