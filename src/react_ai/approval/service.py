"""审批服务：闸门判定、人工决策、超时降级与不可篡改审计."""
import time
from typing import Any, Dict, List, Optional

from react_ai.approval.models import (
    ApprovalRejectedError,
    ApprovalRequest,
    ApprovalRequiredError,
    ApprovalStatus,
)
from react_ai.approval.store import ApprovalStore, AuditChain, InMemoryApprovalStore
from react_ai.config.settings import settings


class ApprovalService:
    def __init__(
        self,
        store: Optional[ApprovalStore] = None,
        timeout_seconds: Optional[float] = None,
    ):
        self.store = store or InMemoryApprovalStore()
        self.audit = AuditChain()
        self.timeout_seconds = (
            timeout_seconds
            if timeout_seconds is not None
            else settings.security.approval_timeout_seconds
        )

    # ---------- 执行闸门（由 ToolSandbox 调用） ----------

    def evaluate_gate(
        self,
        *,
        tool_name: str,
        side_effects: str,
        arguments: Dict[str, Any],
        run_id: str,
        tool_call_id: str,
        tenant_id: str = "default",
        user_id: str = "anonymous",
        trace_id: str = "",
    ) -> None:
        """闸门判定：放行返回 None；需审批抛 ApprovalRequiredError；拒绝/重放抛 ApprovalRejectedError."""
        existing = self.store.find(run_id, tool_call_id)
        if existing is not None:
            self._lazy_expire(existing)
            if existing.status == ApprovalStatus.APPROVED:
                # 消费式放行：一次审批只允许执行一次，防止同键重放
                existing.status = ApprovalStatus.CONSUMED
                self.store.save(existing)
                self.audit.append(existing.id, "consumed", "system", f"审批放行并消费: {tool_name}")
                return
            if existing.status == ApprovalStatus.PENDING:
                raise ApprovalRequiredError(existing)
            raise ApprovalRejectedError(existing)

        request = ApprovalRequest(
            run_id=run_id,
            tool_call_id=tool_call_id,
            tool_name=tool_name,
            arguments=dict(arguments),
            tenant_id=tenant_id,
            user_id=user_id,
            trace_id=trace_id,
            side_effects=side_effects,
            reason=f"工具 [{tool_name}] 副作用为 {side_effects}，需人工审批后方可执行",
            expires_at=time.time() + self.timeout_seconds,
        )
        self.store.save(request)
        self.audit.append(
            request.id, "created", "system",
            f"{tool_name} run={run_id} tool_call={tool_call_id}",
        )
        raise ApprovalRequiredError(request)

    # ---------- 人工决策 ----------

    def decide(
        self,
        request_id: str,
        approved: bool,
        decided_by: str,
        comment: Optional[str] = None,
    ) -> ApprovalRequest:
        """审批人决策入口。仅 PENDING 状态可决策；超时自动降级为拒绝."""
        request = self.store.get(request_id)
        if request is None:
            raise KeyError(f"未找到审批单: {request_id}")
        if request.status != ApprovalStatus.PENDING:
            raise ValueError(
                f"审批单 {request_id} 当前状态为 {request.status.value}，不可重复决策"
            )
        if request.is_expired():
            request.status = ApprovalStatus.EXPIRED
            self.store.save(request)
            self.audit.append(request_id, "expired", "system", "审批超时，按拒绝处理")
            raise ValueError(f"审批单 {request_id} 已超时，按拒绝处理")

        request.status = ApprovalStatus.APPROVED if approved else ApprovalStatus.REJECTED
        request.decided_by = decided_by
        request.decision_comment = comment
        request.decided_at = time.time()
        self.store.save(request)
        self.audit.append(
            request_id,
            "approved" if approved else "rejected",
            decided_by,
            comment or "",
        )
        return request

    # ---------- 查询与审计 ----------

    def get_request(self, request_id: str) -> Optional[ApprovalRequest]:
        request = self.store.get(request_id)
        if request is not None:
            self._lazy_expire(request)
        return request

    def list_requests(self, status: Optional[str] = None) -> List[ApprovalRequest]:
        enum_status = ApprovalStatus(status) if status else None
        return self.store.list(enum_status)

    def verify_audit(self) -> bool:
        return self.audit.verify()

    def audit_records(self) -> List[Dict]:
        return self.audit.records()

    # ---------- 内部 ----------

    def _lazy_expire(self, request: ApprovalRequest) -> None:
        """惰性超时：读取时判定，过期即降级为 EXPIRED（拒绝语义）."""
        if request.is_expired():
            request.status = ApprovalStatus.EXPIRED
            self.store.save(request)
            self.audit.append(request.id, "expired", "system", "审批超时，按拒绝处理")


_default_service: Optional[ApprovalService] = None


def get_approval_service() -> ApprovalService:
    """进程级单例，供沙箱闸门与审批 API 共享."""
    global _default_service
    if _default_service is None:
        _default_service = ApprovalService()
    return _default_service


def reset_approval_service(service: Optional[ApprovalService] = None) -> ApprovalService:
    """注入新实例（测试/多租户场景），返回生效实例."""
    global _default_service
    _default_service = service if service is not None else ApprovalService()
    return _default_service
