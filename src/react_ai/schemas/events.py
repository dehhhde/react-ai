"""运行时事件协议定义 (用于流式输出与审计)."""
from enum import Enum
from typing import Any, Dict, Optional
from pydantic import BaseModel, Field
import time


class AgentEventType(str, Enum):
    RUN_START = "run_start"
    THOUGHT_START = "thought_start"
    THOUGHT_CHUNK = "thought_chunk"
    ACTION_PLAN = "action_plan"
    TOOL_START = "tool_start"
    TOOL_END = "tool_end"
    TOOL_ERROR = "tool_error"
    OBSERVATION = "observation"
    MESSAGE_CHUNK = "message_chunk"
    RUN_END = "run_end"
    ERROR = "error"


class AgentEvent(BaseModel):
    event_id: str
    event_type: AgentEventType
    step: int = 0
    payload: Dict[str, Any] = Field(default_factory=dict)
    timestamp: float = Field(default_factory=time.time)
