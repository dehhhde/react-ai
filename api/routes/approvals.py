"""HITL 人工审批 REST 端点：查询审批单、人工决策、审计链校验."""
from typing import Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from react_ai.approval.models import ApprovalStatus
from react_ai.approval.service import get_approval_service

router = APIRouter(prefix="/api/v1/approvals", tags=["HITL Approval"])


class ApprovalDecisionRequest(BaseModel):
    approved: bool = Field(..., description="true=批准执行, false=拒绝")
    decided_by: str = Field(..., description="审批人身份标识")
    comment: Optional[str] = Field(default=None, description="审批意见")


@router.get("")
async def list_approvals(status: Optional[str] = None):
    """按状态列出审批单，默认全部；status 取值: pending/approved/rejected/expired/consumed."""
    if status:
        try:
            ApprovalStatus(status)
        except ValueError:
            raise HTTPException(status_code=422, detail=f"非法审批状态: {status}")
    requests = get_approval_service().list_requests(status)
    return [r.model_dump() for r in requests]


@router.get("/audit/verify")
async def verify_audit_chain():
    """校验审批审计哈希链完整性（不可篡改证明）."""
    return {"valid": get_approval_service().verify_audit()}


@router.get("/audit/records")
async def list_audit_records():
    return get_approval_service().audit_records()


@router.get("/{request_id}")
async def get_approval(request_id: str):
    request = get_approval_service().get_request(request_id)
    if request is None:
        raise HTTPException(status_code=404, detail=f"未找到审批单: {request_id}")
    return request.model_dump()


@router.post("/{request_id}/decision")
async def decide_approval(request_id: str, body: ApprovalDecisionRequest):
    """审批人决策端点。批准后需调用 /api/v1/agent/runs/{run_id}/resume 恢复执行."""
    svc = get_approval_service()
    try:
        request = svc.decide(
            request_id,
            approved=body.approved,
            decided_by=body.decided_by,
            comment=body.comment,
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    return request.model_dump()
