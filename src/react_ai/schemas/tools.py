"""工具元数据与定义规范."""
from typing import Any, Dict, Optional
from pydantic import BaseModel, Field


class ToolParameterSchema(BaseModel):
    type: str = "object"
    properties: Dict[str, Any] = Field(default_factory=dict)
    required: list[str] = Field(default_factory=list)


class ToolDefinition(BaseModel):
    name: str
    description: str
    parameters: ToolParameterSchema
    requires_approval: bool = False
    timeout_seconds: float = 30.0
    metadata: Dict[str, Any] = Field(default_factory=dict)


class ToolExecutionResult(BaseModel):
    success: bool
    output: Any
    error: Optional[str] = None
    duration_ms: float = 0.0
