"""运行时上下文与控制信号传递."""
import uuid
from typing import Any, Dict, Optional
from pydantic import BaseModel, Field


class RunContext(BaseModel):
    session_id: str = Field(default_factory=lambda: str(uuid.uuid4()), description="会话ID")
    run_id: str = Field(default_factory=lambda: str(uuid.uuid4()), description="单次运行TraceID")
    user_id: Optional[str] = Field(default=None, description="操作用户ID")
    metadata: Dict[str, Any] = Field(default_factory=dict, description="跨环节附加元数据")
    budget_tokens: int = Field(default=100_000, description="本轮运行最大Token预算")
    used_tokens: int = Field(default=0, description="已消耗Token数")
    is_cancelled: bool = Field(default=False, description="协作式取消信号")

    def cancel(self) -> None:
        self.is_cancelled = True

    def check_cancelled(self) -> None:
        if self.is_cancelled:
            raise TimeoutError("Agent 运行已被调用方主动取消或超时中断。")
