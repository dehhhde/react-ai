"""运行断点存储：HITL 挂起时保存 ReAct 循环状态，供审批后恢复续跑."""
import time
from abc import ABC, abstractmethod
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field

from react_ai.schemas.agent import StepResult
from react_ai.schemas.messages import ChatMessage


class RunCheckpoint(BaseModel):
    """一次挂起运行的完整快照."""

    run_id: str
    session_id: str
    user_id: Optional[str] = None
    metadata: Dict[str, Any] = Field(default_factory=dict)
    current_step: int = 0
    messages: List[ChatMessage] = Field(default_factory=list)
    step_history: List[StepResult] = Field(default_factory=list)
    # 未执行完的工具调用: [{"tool_call": {...}, "approval_id": str | None}]
    pending_tool_calls: List[Dict[str, Any]] = Field(default_factory=list)
    created_at: float = Field(default_factory=time.time)


class CheckpointStore(ABC):
    @abstractmethod
    def save(self, checkpoint: RunCheckpoint) -> None: ...

    @abstractmethod
    def load(self, run_id: str) -> Optional[RunCheckpoint]: ...

    @abstractmethod
    def delete(self, run_id: str) -> None: ...


class InMemoryCheckpointStore(CheckpointStore):
    def __init__(self):
        self._checkpoints: Dict[str, RunCheckpoint] = {}

    def save(self, checkpoint: RunCheckpoint) -> None:
        self._checkpoints[checkpoint.run_id] = checkpoint

    def load(self, run_id: str) -> Optional[RunCheckpoint]:
        return self._checkpoints.get(run_id)

    def delete(self, run_id: str) -> None:
        self._checkpoints.pop(run_id, None)


_default_store: Optional[CheckpointStore] = None


def get_checkpoint_store() -> CheckpointStore:
    """进程级单例，供引擎与 resume API 共享."""
    global _default_store
    if _default_store is None:
        _default_store = InMemoryCheckpointStore()
    return _default_store
