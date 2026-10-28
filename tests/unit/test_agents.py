"""BaseAgent 基类与契约单元测试."""
import time
import unittest
from typing import Any, ClassVar

from react_ai.agents.base import (
    AgentContext,
    AgentResult,
    AgentStatus,
    AgentStep,
    BaseAgent,
)


class SimpleEchoAgent(BaseAgent):
    name = "simple_echo_agent"
    description = "简单的单步回显智能体"
    version = "1.0.0"
    tags: ClassVar[list[str]] = ["test", "echo"]

    def _step(self, step_number: int, input_data: dict[str, Any], ctx: AgentContext) -> AgentStep:
        return AgentStep(
            step_number=step_number,
            action="respond",
            input_data=input_data,
            output_data={"content": f"Echo: {input_data.get('query')}"},
            duration_ms=5,
        )


class MultiStepWorkflowAgent(BaseAgent):
    name = "multi_step_workflow_agent"
    description = "多步测试智能体 (think -> call_tool -> respond)"
    tags: ClassVar[list[str]] = ["test", "workflow"]

    def _step(self, step_number: int, input_data: dict[str, Any], ctx: AgentContext) -> AgentStep:
        if step_number == 1:
            return AgentStep(
                step_number=step_number,
                action="think",
                input_data={"thought": "分析用户问题"},
                output_data={"thought": "用户需要查询订单"},
                duration_ms=10,
            )
        elif step_number == 2:
            return AgentStep(
                step_number=step_number,
                action="call_tool",
                input_data={"tool": "query_order", "order_id": input_data.get("order_id")},
                output_data={"order_status": "DELIVERED"},
                duration_ms=25,
            )
        else:
            return AgentStep(
                step_number=step_number,
                action="respond",
                input_data={},
                output_data={"content": "订单已送达"},
                duration_ms=15,
            )


class WaitingAgent(BaseAgent):
    name = "waiting_agent"

    def _step(self, step_number: int, input_data: dict[str, Any], ctx: AgentContext) -> AgentStep:
        return AgentStep(
            step_number=step_number,
            action="wait",
            input_data=input_data,
            output_data={"content": "等待人工审批中"},
            duration_ms=8,
        )


class InfiniteLoopAgent(BaseAgent):
    name = "infinite_loop_agent"

    def _step(self, step_number: int, input_data: dict[str, Any], ctx: AgentContext) -> AgentStep:
        return AgentStep(
            step_number=step_number,
            action="think",
            input_data={},
            output_data={"thought": "循环思考中..."},
            duration_ms=2,
        )


class SlowTimeoutAgent(BaseAgent):
    name = "slow_timeout_agent"

    def _step(self, step_number: int, input_data: dict[str, Any], ctx: AgentContext) -> AgentStep:
        time.sleep(0.01)
        return AgentStep(
            step_number=step_number,
            action="think",
            input_data={},
            output_data={"thought": "慢速步骤"},
            duration_ms=10,
        )


class FaultyAgent(BaseAgent):
    name = "faulty_agent"

    def _step(self, step_number: int, input_data: dict[str, Any], ctx: AgentContext) -> AgentStep:
        raise RuntimeError("底层工具执行失败")


class DefaultTagsAgent(BaseAgent):
    name = "default_tags_agent"

    def _step(self, step_number: int, input_data: dict[str, Any], ctx: AgentContext) -> AgentStep:
        return AgentStep(
            step_number=step_number,
            action="respond",
            input_data={},
            output_data={"content": "ok"},
            duration_ms=1,
        )


class TestBaseAgent(unittest.TestCase):
    def test_agent_status_enum(self):
        self.assertEqual(AgentStatus.IDLE.value, "idle")
        self.assertEqual(AgentStatus.RUNNING.value, "running")
        self.assertEqual(AgentStatus.COMPLETED.value, "completed")
        self.assertEqual(AgentStatus.FAILED.value, "failed")
        self.assertEqual(AgentStatus.WAITING.value, "waiting")

    def test_agent_context_defaults(self):
        ctx = AgentContext()
        self.assertTrue(len(ctx.trace_id) >= 16)
        self.assertEqual(ctx.max_steps, 20)
        self.assertEqual(ctx.timeout_seconds, 300.0)
        self.assertIsNone(ctx.session_id)
        self.assertIsNone(ctx.user_id)
        self.assertIsNone(ctx.tenant_id)
        self.assertEqual(ctx.metadata, {})

    def test_agent_context_custom_fields(self):
        ctx = AgentContext(
            trace_id="custom-trace-001",
            session_id="session-abc",
            user_id="USR_001",
            tenant_id="tenant-99",
            max_steps=5,
            timeout_seconds=10.0,
            metadata={"channel": "web"},
        )
        self.assertEqual(ctx.trace_id, "custom-trace-001")
        self.assertEqual(ctx.session_id, "session-abc")
        self.assertEqual(ctx.user_id, "USR_001")
        self.assertEqual(ctx.tenant_id, "tenant-99")
        self.assertEqual(ctx.max_steps, 5)
        self.assertEqual(ctx.timeout_seconds, 10.0)
        self.assertEqual(ctx.metadata["channel"], "web")

    def test_base_agent_initialization(self):
        agent = SimpleEchoAgent()
        self.assertEqual(agent.name, "simple_echo_agent")
        self.assertEqual(agent.description, "简单的单步回显智能体")
        self.assertEqual(agent.version, "1.0.0")
        self.assertEqual(agent.tags, ["test", "echo"])
        self.assertEqual(agent.status, AgentStatus.IDLE)
        self.assertEqual(agent.steps, [])
        self.assertIsNone(agent.context)

    def test_default_tags_is_list(self):
        agent = DefaultTagsAgent()
        self.assertIsInstance(agent.tags, list)
        self.assertEqual(agent.tags, [])

    def test_successful_run(self):
        agent = SimpleEchoAgent()
        ctx = AgentContext(session_id="sess_001", user_id="u_001")
        result = agent.run({"query": "你好"}, ctx)

        self.assertIsInstance(result, AgentResult)
        self.assertTrue(result.ok)
        self.assertEqual(result.output, "Echo: 你好")
        self.assertEqual(result.status, AgentStatus.COMPLETED)
        self.assertEqual(agent.status, AgentStatus.COMPLETED)
        self.assertEqual(len(result.steps), 1)
        self.assertEqual(result.steps[0].action, "respond")
        self.assertEqual(result.trace_id, ctx.trace_id)
        self.assertIsNone(result.error_message)
        self.assertGreaterEqual(result.total_duration_ms, 0)

        # 检查步骤列表副本防篡改
        steps_copy = agent.steps
        steps_copy.clear()
        self.assertEqual(len(agent.steps), 1)

    def test_multi_step_workflow(self):
        agent = MultiStepWorkflowAgent()
        ctx = AgentContext(max_steps=10)
        result = agent.run({"order_id": "ORD_001"}, ctx)

        self.assertIsInstance(result, AgentResult)
        self.assertTrue(result.ok)
        self.assertEqual(result.output, "订单已送达")
        self.assertEqual(result.status, AgentStatus.COMPLETED)
        self.assertEqual(len(result.steps), 3)
        self.assertEqual(result.steps[0].action, "think")
        self.assertEqual(result.steps[1].action, "call_tool")
        self.assertEqual(result.steps[2].action, "respond")

    def test_waiting_action(self):
        agent = WaitingAgent()
        result = agent.run({"order_id": "ORD_002"})

        self.assertIsInstance(result, AgentResult)
        self.assertTrue(result.ok)
        self.assertEqual(result.output, "等待人工审批中")
        self.assertEqual(result.status, AgentStatus.WAITING)
        self.assertEqual(agent.status, AgentStatus.WAITING)
        self.assertEqual(len(result.steps), 1)

    def test_max_steps_exceeded(self):
        agent = InfiniteLoopAgent()
        ctx = AgentContext(max_steps=4)
        result = agent.run({"data": "test"}, ctx)

        self.assertIsInstance(result, AgentResult)
        self.assertFalse(result.ok)
        self.assertIsNone(result.output)
        self.assertEqual(result.status, AgentStatus.FAILED)
        self.assertEqual(agent.status, AgentStatus.FAILED)
        self.assertEqual(len(result.steps), 4)
        self.assertIn("达到最大步数 4", result.error_message)

    def test_timeout_handling(self):
        agent = SlowTimeoutAgent()
        ctx = AgentContext(max_steps=100, timeout_seconds=0.005)
        result = agent.run({"data": "test"}, ctx)

        self.assertIsInstance(result, AgentResult)
        self.assertFalse(result.ok)
        self.assertIsNone(result.output)
        self.assertEqual(result.status, AgentStatus.FAILED)
        self.assertEqual(agent.status, AgentStatus.FAILED)
        self.assertEqual(result.error_message, "Agent 执行超时")

    def test_exception_handling(self):
        agent = FaultyAgent()
        result = agent.run({"data": "test"})

        self.assertIsInstance(result, AgentResult)
        self.assertFalse(result.ok)
        self.assertIsNone(result.output)
        self.assertEqual(result.status, AgentStatus.FAILED)
        self.assertEqual(agent.status, AgentStatus.FAILED)
        self.assertEqual(result.error_message, "底层工具执行失败")

    def test_empty_input_data_raises_value_error(self):
        agent = SimpleEchoAgent()
        result = agent.run({})

        self.assertIsInstance(result, AgentResult)
        self.assertFalse(result.ok)
        self.assertEqual(result.status, AgentStatus.FAILED)
        self.assertIn("input_data 不能为空", result.error_message)

    def test_reset(self):
        agent = SimpleEchoAgent()
        agent.run({"query": "测试重置"})
        self.assertEqual(agent.status, AgentStatus.COMPLETED)
        self.assertEqual(len(agent.steps), 1)
        self.assertIsNotNone(agent.context)

        agent.reset()
        self.assertEqual(agent.status, AgentStatus.IDLE)
        self.assertEqual(len(agent.steps), 0)
        self.assertIsNone(agent.context)


if __name__ == "__main__":
    unittest.main()
