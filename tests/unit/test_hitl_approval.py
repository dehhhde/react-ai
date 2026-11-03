"""HITL 人机审批闭环单元测试：闸门拦截、挂起、审批放行/拒绝、超时降级、重放防护、审计链."""
import unittest
from typing import Any

from react_ai.approval.models import (
    ApprovalRejectedError,
    ApprovalRequiredError,
    ApprovalStatus,
)
from react_ai.approval.service import ApprovalService
from react_ai.core.checkpoint import InMemoryCheckpointStore
from react_ai.core.context import RunContext
from react_ai.core.engine import ReActAgent
from react_ai.llm.base import BaseLLMClient, LLMResponse
from react_ai.schemas.events import AgentEventType
from react_ai.schemas.messages import MessageRole, ToolCall
from react_ai.tools.base import BaseTool
from react_ai.tools.registry import ToolRegistry
from react_ai.tools.sandbox import ToolSandbox


class RefundTool(BaseTool):
    name = "create_refund_test"
    description = "测试退款写工具"
    side_effects = "write"

    @property
    def input_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {"order_id": {"type": "string"}},
            "required": ["order_id"],
        }

    def execute(self, arguments: dict, ctx) -> Any:
        return {"ticket_id": "TKT_1", "amount": 99}


class ReadTool(BaseTool):
    name = "query_order_test"
    description = "测试只读查询工具"
    side_effects = "read"

    @property
    def input_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {"order_id": {"type": "string"}},
            "required": ["order_id"],
        }

    def execute(self, arguments: dict, ctx) -> Any:
        return {"order_id": arguments.get("order_id"), "status": "DELIVERED"}


class ApprovalFlowLLM(BaseLLMClient):
    """第一次决策调用写工具；收到 tool 观测后给出最终答复."""

    def __init__(self, tool_call_id: str = "call_w1"):
        self.tool_call_id = tool_call_id
        self.calls = 0

    async def generate(self, messages, tools=None, temperature=0.2, max_tokens=None, tool_choice=None):
        self.calls += 1
        has_tool_msg = any(m.role == MessageRole.TOOL for m in messages)
        if not has_tool_msg:
            return LLMResponse(
                content="需要发起退款",
                tool_calls=[ToolCall(
                    id=self.tool_call_id,
                    name="create_refund_test",
                    arguments={"order_id": "ORD_1"},
                )],
            )
        last_tool = [m.content for m in messages if m.role == MessageRole.TOOL][-1]
        return LLMResponse(content=f"最终答复，观测={last_tool}")

    async def stream_generate(self, messages, tools=None, temperature=0.2):
        yield "mock"


def build_agent(timeout_seconds: float = 3600.0):
    svc = ApprovalService(timeout_seconds=timeout_seconds)
    registry = ToolRegistry(sandbox=ToolSandbox(approval_service=svc))
    registry.register(RefundTool())
    registry.register(ReadTool())
    llm = ApprovalFlowLLM()
    agent = ReActAgent(
        llm=llm,
        tool_registry=registry,
        approval_service=svc,
        checkpoint_store=InMemoryCheckpointStore(),
    )
    return agent, svc, registry, llm


def build_context() -> RunContext:
    return RunContext(metadata={"idempotency_key": "k1"})


class TestApprovalGate(unittest.IsolatedAsyncioTestCase):
    async def test_read_tool_passes_without_approval(self):
        _, svc, registry, _ = build_agent()
        data = await registry.execute("query_order_test", {"order_id": "ORD_1"}, build_context())
        self.assertEqual(data["status"], "DELIVERED")
        self.assertEqual(svc.list_requests(), [])

    async def test_write_tool_gate_creates_pending_request(self):
        _, svc, registry, _ = build_agent()
        with self.assertRaises(ApprovalRequiredError) as cm:
            await registry.execute("create_refund_test", {"order_id": "ORD_1"}, build_context())
        req = cm.exception.request
        self.assertEqual(req.status, ApprovalStatus.PENDING)
        self.assertEqual(req.tool_name, "create_refund_test")
        self.assertEqual(len(svc.list_requests("pending")), 1)

    async def test_replay_blocked_after_consumed(self):
        _, svc, registry, _ = build_agent()
        ctx = build_context()
        with self.assertRaises(ApprovalRequiredError):
            await registry.execute("create_refund_test", {"order_id": "ORD_1"}, ctx, tool_call_id="tc_replay")
        req = svc.list_requests("pending")[0]
        svc.decide(req.id, approved=True, decided_by="admin")
        await registry.execute("create_refund_test", {"order_id": "ORD_1"}, ctx, tool_call_id="tc_replay")
        # 同键二次执行必须被拒绝（审批一次性消费）
        with self.assertRaises(ApprovalRejectedError) as cm:
            await registry.execute("create_refund_test", {"order_id": "ORD_1"}, ctx, tool_call_id="tc_replay")
        self.assertEqual(cm.exception.request.status, ApprovalStatus.CONSUMED)


class TestApprovalService(unittest.TestCase):
    def test_expiry_downgrades_to_reject(self):
        svc = ApprovalService(timeout_seconds=-1)
        with self.assertRaises(ApprovalRequiredError) as cm:
            svc.evaluate_gate(
                tool_name="t", side_effects="write", arguments={},
                run_id="r1", tool_call_id="c1",
            )
        req_id = cm.exception.request.id
        with self.assertRaises(ValueError):
            svc.decide(req_id, approved=True, decided_by="admin")
        self.assertEqual(svc.get_request(req_id).status, ApprovalStatus.EXPIRED)

    def test_double_decision_rejected(self):
        svc = ApprovalService(timeout_seconds=3600)
        with self.assertRaises(ApprovalRequiredError) as cm:
            svc.evaluate_gate(
                tool_name="t", side_effects="write", arguments={},
                run_id="r1", tool_call_id="c1",
            )
        req_id = cm.exception.request.id
        svc.decide(req_id, approved=True, decided_by="admin")
        with self.assertRaises(ValueError):
            svc.decide(req_id, approved=False, decided_by="admin2")

    def test_audit_chain_tamper_detection(self):
        svc = ApprovalService(timeout_seconds=3600)
        with self.assertRaises(ApprovalRequiredError):
            svc.evaluate_gate(
                tool_name="t", side_effects="write", arguments={},
                run_id="r1", tool_call_id="c1",
            )
        self.assertTrue(svc.verify_audit())
        svc.audit._records[0]["detail"] = "tampered"
        self.assertFalse(svc.verify_audit())


class TestEnginePauseResume(unittest.IsolatedAsyncioTestCase):
    async def _collect(self, agen):
        events = []
        async for e in agen:
            events.append(e)
        return events

    async def test_stream_pauses_on_write_tool(self):
        agent, svc, _, _ = build_agent()
        ctx = build_context()
        events = await self._collect(agent.stream("给 ORD_1 退款", ctx))
        types = [e.event_type for e in events]
        self.assertIn(AgentEventType.TOOL_APPROVAL_REQUIRED, types)
        self.assertEqual(types[-1], AgentEventType.RUN_PAUSED)
        self.assertTrue(ctx.is_paused)
        approval_event = events[types.index(AgentEventType.TOOL_APPROVAL_REQUIRED)]
        self.assertEqual(approval_event.payload["tool"], "create_refund_test")
        self.assertEqual(len(svc.list_requests("pending")), 1)
        # 挂起后检查点已保存
        self.assertIsNotNone(agent.checkpoints.load(ctx.run_id))

    async def test_run_returns_awaiting_approval(self):
        agent, _, _, _ = build_agent()
        response = await agent.run("给 ORD_1 退款", build_context())
        self.assertTrue(response.success)
        self.assertEqual(response.status, "awaiting_approval")
        self.assertEqual(len(response.pending_approval_ids), 1)

    async def test_approve_then_resume_executes(self):
        agent, svc, _, llm = build_agent()
        ctx = build_context()
        await self._collect(agent.stream("给 ORD_1 退款", ctx))
        req = svc.list_requests("pending")[0]
        svc.decide(req.id, approved=True, decided_by="admin_01", comment="核实无误")

        events = await self._collect(agent.resume(ctx.run_id))
        types = [e.event_type for e in events]
        self.assertEqual(types[0], AgentEventType.RUN_RESUMED)
        self.assertIn(AgentEventType.TOOL_END, types)
        self.assertEqual(types[-1], AgentEventType.RUN_END)

        tool_end = events[types.index(AgentEventType.TOOL_END)]
        self.assertEqual(tool_end.payload["result"]["ticket_id"], "TKT_1")
        run_end = events[-1]
        self.assertIn("TKT_1", run_end.payload["output"])
        # 审批单已消费、检查点已清理
        self.assertEqual(svc.get_request(req.id).status, ApprovalStatus.CONSUMED)
        self.assertIsNone(agent.checkpoints.load(ctx.run_id))
        self.assertEqual(llm.calls, 2)

    async def test_reject_then_resume_feeds_rejection(self):
        agent, svc, _, _ = build_agent()
        ctx = build_context()
        await self._collect(agent.stream("给 ORD_1 退款", ctx))
        req = svc.list_requests("pending")[0]
        svc.decide(req.id, approved=False, decided_by="admin_01", comment="金额不符")

        events = await self._collect(agent.resume(ctx.run_id))
        types = [e.event_type for e in events]
        self.assertIn(AgentEventType.TOOL_APPROVAL_DECISION, types)
        self.assertNotIn(AgentEventType.TOOL_END, types)
        self.assertEqual(types[-1], AgentEventType.RUN_END)
        run_end = events[-1]
        self.assertIn("审批未通过", run_end.payload["output"])
        self.assertIn("金额不符", run_end.payload["output"])
        self.assertIsNone(agent.checkpoints.load(ctx.run_id))

    async def test_resume_while_still_pending_keeps_paused(self):
        agent, svc, _, _ = build_agent()
        ctx = build_context()
        await self._collect(agent.stream("给 ORD_1 退款", ctx))
        events = await self._collect(agent.resume(ctx.run_id))
        types = [e.event_type for e in events]
        self.assertEqual(types[-1], AgentEventType.RUN_PAUSED)
        # 检查点保留，可再次恢复
        self.assertIsNotNone(agent.checkpoints.load(ctx.run_id))


if __name__ == "__main__":
    unittest.main()
