"""HITL 人机审批领域模型与异常."""
import time
import uuid
from enum import Enum
from typing import Any, Dict, Optional

from pydantic import BaseModel, Field


class ApprovalStatus(str, Enum):
    PENDING = "pending"      # 等待人工决策
    APPROVED = "approved"    # 已批准，等待闸门消费放行
    REJECTED = "rejected"    # 人工拒绝
    EXPIRED = "expired"      # 审批超时，按拒绝处理
    CONSUMED = "consumed"    # 审批已放行且工具已执行，防止同键重放


class ApprovalRequest(BaseModel):
    """一次待人工决策的工具调用审批单."""

    id: str = Field(default_factory=lambda: uuid.uuid4().hex)
    run_id: str
    tool_call_id: str
    tool_name: str
    arguments: Dict[str, Any] = Field(default_factory=dict)
    tenant_id: str = "default"
    user_id: str = "anonymous"
    trace_id: str = ""
    side_effects: str = "none"
    reason: str = ""
    status: ApprovalStatus = ApprovalStatus.PENDING
    created_at: float = Field(default_factory=time.time)
    expires_at: float = 0.0
    decided_by: Optional[str] = None
    decision_comment: Optional[str] = None
    decided_at: Optional[float] = None

    def is_expired(self, now: Optional[float] = None) -> bool:
        """仅未决策的请求参与超时判定."""
        if self.status != ApprovalStatus.PENDING or not self.expires_at:
            return False
        return (now if now is not None else time.time()) > self.expires_at


class ApprovalRequiredError(Exception):
    """闸门信号：工具调用需要人工审批，携带审批单供引擎挂起运行."""

    def __init__(self, request: ApprovalRequest):
        self.request = request
        super().__init__(
            f"工具 [{request.tool_name}] 需要人工审批 (approval_id={request.id})"
        )


class ApprovalRejectedError(Exception):
    """闸门信号：审批已被拒绝/超时/已消费，禁止执行."""

    def __init__(self, request: ApprovalRequest):
        self.request = request
        super().__init__(
            f"工具 [{request.tool_name}] 审批未通过 (status={request.status.value})"
        )
