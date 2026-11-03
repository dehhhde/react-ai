"""审批请求存储与防篡改审计链."""
import hashlib
import time
from abc import ABC, abstractmethod
from typing import Dict, List, Optional

from react_ai.approval.models import ApprovalRequest, ApprovalStatus


class ApprovalStore(ABC):
    """审批单存储抽象（生产环境可替换为 Redis/PostgreSQL 实现）."""

    @abstractmethod
    def save(self, request: ApprovalRequest) -> None: ...

    @abstractmethod
    def get(self, request_id: str) -> Optional[ApprovalRequest]: ...

    @abstractmethod
    def find(self, run_id: str, tool_call_id: str) -> Optional[ApprovalRequest]: ...

    @abstractmethod
    def list(self, status: Optional[ApprovalStatus] = None) -> List[ApprovalRequest]: ...


class InMemoryApprovalStore(ApprovalStore):
    def __init__(self):
        self._requests: Dict[str, ApprovalRequest] = {}

    def save(self, request: ApprovalRequest) -> None:
        self._requests[request.id] = request

    def get(self, request_id: str) -> Optional[ApprovalRequest]:
        return self._requests.get(request_id)

    def find(self, run_id: str, tool_call_id: str) -> Optional[ApprovalRequest]:
        for req in self._requests.values():
            if req.run_id == run_id and req.tool_call_id == tool_call_id:
                return req
        return None

    def list(self, status: Optional[ApprovalStatus] = None) -> List[ApprovalRequest]:
        items = sorted(self._requests.values(), key=lambda r: r.created_at)
        if status is not None:
            items = [r for r in items if r.status == status]
        return items


class AuditChain:
    """append-only 哈希链审计账本.

    每条记录包含前一条的哈希，任何对历史记录的改写或删除
    都会导致 verify() 校验失败，实现审批轨迹不可篡改。
    """

    GENESIS = "GENESIS"

    def __init__(self):
        self._records: List[Dict] = []

    def append(self, request_id: str, action: str, actor: str, detail: str) -> Dict:
        prev_hash = self._records[-1]["record_hash"] if self._records else self.GENESIS
        seq = len(self._records)
        at = time.time()
        record = {
            "seq": seq,
            "request_id": request_id,
            "action": action,
            "actor": actor,
            "at": at,
            "detail": detail,
            "prev_hash": prev_hash,
        }
        record["record_hash"] = self._hash(record)
        self._records.append(record)
        return record

    def records(self) -> List[Dict]:
        return [dict(r) for r in self._records]

    def verify(self) -> bool:
        """重算哈希链，校验账本完整性."""
        prev_hash = self.GENESIS
        for rec in self._records:
            if rec["prev_hash"] != prev_hash:
                return False
            if rec["record_hash"] != self._hash(rec):
                return False
            prev_hash = rec["record_hash"]
        return True

    @staticmethod
    def _hash(record: Dict) -> str:
        payload = (
            f"{record['seq']}|{record['request_id']}|{record['action']}|"
            f"{record['actor']}|{record['at']:.6f}|{record['detail']}|{record['prev_hash']}"
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()
