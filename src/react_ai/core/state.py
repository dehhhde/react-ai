"""智能体状态机与历史快照."""
from typing import List, Optional
from pydantic import BaseModel, Field
from react_ai.schemas.messages import ChatMessage
from react_ai.schemas.agent import StepResult


class AgentState(BaseModel):
    current_step: int = 0
    messages: List[ChatMessage] = Field(default_factory=list)
    step_history: List[StepResult] = Field(default_factory=list)
    is_finished: bool = False
    final_output: Optional[str] = None
    error: Optional[str] = None
    consecutive_tool_failures: int = 0  # 连续工具失败计数，成功一次即归零

    def add_step(self, step: StepResult) -> None:
        self.step_history.append(step)
        self.current_step += 1
