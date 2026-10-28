"""BaseFlow 流程编排基类、执行上下文与结果契约单元测试."""
import asyncio
import time
import unittest

from react_ai.flows.base import (
    BaseFlow,
    FlowContext,
    FlowPriority,
    FlowResult,
    FlowStatus,
    StepResult,
)


class SimpleLinearFlow(BaseFlow):
    name = "simple_linear_flow"
    version = "1.0.0"

    def build_steps(self):
        self.register_step("step_1", self.step_one)
        self.register_step("step_2", self.step_two)

    def step_one(self, ctx: FlowContext):
        val = ctx.input_data.get("value", 0)
        return val * 2

    def step_two(self, ctx: FlowContext):
        prev = ctx.shared_state.get("step_1", 0)
        ctx.output_data["result"] = prev + 10
        return StepResult(step_id="step_2", success=True, data=ctx.output_data["result"])


class AsyncLinearFlow(BaseFlow):
    name = "async_linear_flow"

    def build_steps(self):
        self.register_step("fetch_user", self.fetch_user)
        self.register_step("enrich_data", self.enrich_data)

    async def fetch_user(self, ctx: FlowContext):
        await asyncio.sleep(0.01)
        user_id = ctx.input_data.get("user_id", "u_unknown")
        return {"user_id": user_id, "name": "Alice"}

    def enrich_data(self, ctx: FlowContext):
        user = ctx.shared_state.get("fetch_user", {})
        ctx.output_data["profile"] = {**user, "vip": True}
        return ctx.output_data["profile"]


class RetryTestFlow(BaseFlow):
    name = "retry_test_flow"
    retry_delay_seconds = 0.01

    def __init__(self, fail_count_target=2):
        self.fail_count_target = fail_count_target
        self.attempts = 0
        super().__init__()

    def build_steps(self):
        self.register_step("flaky_step", self.flaky_action, retries=3)

    def flaky_action(self, ctx: FlowContext):
        self.attempts += 1
        if self.attempts <= self.fail_count_target:
            raise ValueError(f"Temporary failure attempt {self.attempts}")
        return "success_after_retry"


class TestFlowEnumsAndDataContracts(unittest.TestCase):
    def test_flow_status_enum(self):
        self.assertEqual(FlowStatus.PENDING.value, "pending")
        self.assertEqual(FlowStatus.RUNNING.value, "running")
        self.assertEqual(FlowStatus.COMPLETED.value, "completed")
        self.assertEqual(FlowStatus.FAILED.value, "failed")
        self.assertEqual(FlowStatus.CANCELLED.value, "cancelled")
        self.assertEqual(FlowStatus.TIMEOUT.value, "timeout")

    def test_flow_priority_enum(self):
        self.assertEqual(FlowPriority.LOW.value, 0)
        self.assertEqual(FlowPriority.MEDIUM.value, 1)
        self.assertEqual(FlowPriority.HIGH.value, 2)
        self.assertEqual(FlowPriority.CRITICAL.value, 3)

    def test_flow_context_defaults(self):
        ctx = FlowContext()
        self.assertTrue(len(ctx.flow_id) >= 16)
        self.assertEqual(ctx.request_id, "")
        self.assertEqual(ctx.session_id, "")
        self.assertIsNone(ctx.user_id)
        self.assertIsNone(ctx.tenant_id)
        self.assertEqual(ctx.metadata, {})
        self.assertEqual(ctx.input_data, {})
        self.assertEqual(ctx.output_data, {})
        self.assertEqual(ctx.shared_state, {})
        self.assertEqual(ctx.errors, {}) if isinstance(ctx.errors, dict) else self.assertEqual(ctx.errors, [])
        self.assertEqual(ctx.start_time, 0.0)
        self.assertEqual(ctx.end_time, 0.0)
        self.assertEqual(ctx.get_elapsed_ms(), 0.0)

    def test_flow_context_elapsed_and_to_dict(self):
        ctx = FlowContext(
            request_id="req-123",
            session_id="sess-456",
            user_id="user-789",
            tenant_id="tenant-001",
            metadata={"env": "test"},
            input_data={"query": "test query"},
            start_time=time.time() - 0.05,
            end_time=time.time(),
        )
        self.assertGreater(ctx.get_elapsed_ms(), 0.0)
        data = ctx.to_dict()
        self.assertEqual(data["request_id"], "req-123")
        self.assertEqual(data["session_id"], "sess-456")
        self.assertEqual(data["user_id"], "user-789")
        self.assertEqual(data["tenant_id"], "tenant-001")
        self.assertEqual(data["metadata"]["env"], "test")
        self.assertEqual(data["input_data"]["query"], "test query")

    def test_step_result_to_dict(self):
        step_res = StepResult(
            step_id="step_a",
            step_name="Step A",
            success=True,
            data={"key": "val"},
            latency_ms=12.345,
            metadata={"retry": 0},
        )
        d = step_res.to_dict()
        self.assertEqual(d["step_id"], "step_a")
        self.assertEqual(d["step_name"], "Step A")
        self.assertTrue(d["success"])
        self.assertEqual(d["data"], {"key": "val"})
        self.assertEqual(d["latency_ms"], 12.35)
        self.assertEqual(d["metadata"], {"retry": 0})
        self.assertIsNone(d["error"])

    def test_flow_result_to_dict(self):
        res = FlowResult(
            flow_id="flow-999",
            status=FlowStatus.COMPLETED,
            success=True,
            data={"final": 42},
            total_latency_ms=55.678,
            step_results=[
                StepResult(step_id="s1", step_name="s1", success=True, data=42, latency_ms=10.0)
            ],
        )
        d = res.to_dict()
        self.assertEqual(d["flow_id"], "flow-999")
        self.assertEqual(d["status"], "completed")
        self.assertTrue(d["success"])
        self.assertEqual(d["data"], {"final": 42})
        self.assertEqual(d["total_latency_ms"], 55.68)
        self.assertEqual(len(d["step_results"]), 1)


class TestBaseFlowSyncExecution(unittest.TestCase):
    def test_subclass_build_steps_and_execution(self):
        flow = SimpleLinearFlow()
        self.assertEqual(len(flow.steps), 2)
        res = flow.execute(input_data={"value": 5})

        self.assertEqual(res.status, FlowStatus.COMPLETED)
        self.assertTrue(res.success)
        self.assertEqual(res.data, {"result": 20})
        self.assertEqual(len(res.step_results), 2)
        self.assertEqual(res.step_results[0].data, 10)
        self.assertEqual(res.step_results[1].data, 20)
        self.assertGreaterEqual(res.total_latency_ms, 0.0)

    def test_callable_invocation(self):
        flow = SimpleLinearFlow()
        res = flow({"value": 3})
        self.assertTrue(res.success)
        self.assertEqual(res.data, {"result": 16})

    def test_register_and_unregister_step(self):
        flow = BaseFlow()
        flow.register_step("s1", lambda ctx: 1)
        flow.register_step("s2", lambda ctx: 2)
        self.assertEqual(len(flow.steps), 2)

        removed = flow.unregister_step("s1")
        self.assertTrue(removed)
        self.assertEqual(len(flow.steps), 1)

        removed_again = flow.unregister_step("non_existent")
        self.assertFalse(removed_again)

    def test_flow_condition_skip(self):
        flow = BaseFlow()
        flow.register_step("step_always", lambda ctx: "always")
        flow.register_step(
            "step_conditional",
            lambda ctx: "conditional",
            condition=lambda ctx: ctx.input_data.get("flag", False) is True,
        )

        res_skipped = flow.execute(input_data={"flag": False})
        self.assertEqual(len(res_skipped.step_results), 1)
        self.assertEqual(res_skipped.step_results[0].step_id, "step_always")

        res_executed = flow.execute(input_data={"flag": True})
        self.assertEqual(len(res_executed.step_results), 2)
        self.assertEqual(res_executed.step_results[1].step_id, "step_conditional")

    def test_flow_disabled(self):
        flow = SimpleLinearFlow()
        flow.enabled = False
        res = flow.execute(input_data={"value": 5})

        self.assertEqual(res.status, FlowStatus.FAILED)
        self.assertFalse(res.success)
        self.assertEqual(res.error, "flow is disabled")

    def test_flow_short_circuit_on_error(self):
        flow = BaseFlow()
        flow.short_circuit_on_error = True

        def failing_step(ctx):
            raise RuntimeError("Step failed explicitly")

        flow.register_step("step_1", lambda ctx: "ok")
        flow.register_step("step_2", failing_step)
        flow.register_step("step_3", lambda ctx: "should_not_run")

        res = flow.execute()
        self.assertEqual(res.status, FlowStatus.FAILED)
        self.assertFalse(res.success)
        self.assertEqual(len(res.step_results), 2)
        self.assertTrue(res.step_results[0].success)
        self.assertFalse(res.step_results[1].success)
        self.assertIn("Step failed explicitly", res.error)

    def test_flow_non_short_circuit_on_error(self):
        flow = BaseFlow()
        flow.short_circuit_on_error = False

        flow.register_step("step_1", lambda ctx: 1)
        flow.register_step("step_2", lambda ctx: (_ for _ in ()).throw(RuntimeError("fail")))
        flow.register_step("step_3", lambda ctx: 3)

        res = flow.execute()
        self.assertEqual(res.status, FlowStatus.RUNNING)
        self.assertFalse(res.success)
        self.assertEqual(len(res.step_results), 3)
        self.assertTrue(res.step_results[0].success)
        self.assertFalse(res.step_results[1].success)
        self.assertTrue(res.step_results[2].success)

    def test_flow_retry_mechanism_success(self):
        flow = RetryTestFlow(fail_count_target=2)
        res = flow.execute()
        self.assertTrue(res.success)
        self.assertEqual(flow.attempts, 3)

    def test_flow_retry_exhausted(self):
        flow = RetryTestFlow(fail_count_target=5)
        res = flow.execute()
        self.assertFalse(res.success)
        self.assertEqual(res.status, FlowStatus.FAILED)
        self.assertEqual(flow.attempts, 4)

    def test_lifecycle_hooks(self):
        flow = BaseFlow()
        events = []

        flow.add_pre_hook(lambda ctx: events.append("pre_flow"))
        flow.add_post_hook(lambda ctx, res: events.append(f"post_flow_{res.status.value}"))
        flow.add_step_pre_hook(lambda sid, ctx: events.append(f"pre_step_{sid}"))
        flow.add_step_post_hook(lambda sid, sres, ctx: events.append(f"post_step_{sid}_{sres.success}"))

        flow.register_step("s1", lambda ctx: "step_one")
        res = flow.execute()

        self.assertTrue(res.success)
        self.assertEqual(
            events,
            [
                "pre_flow",
                "pre_step_s1",
                "post_step_s1_True",
                "post_flow_completed",
            ],
        )

    def test_hook_exception_tolerance(self):
        flow = BaseFlow()

        def broken_pre_hook(ctx):
            raise RuntimeError("pre-hook crash")

        flow.add_pre_hook(broken_pre_hook)
        flow.register_step("s1", lambda ctx: "ok")
        res = flow.execute()
        self.assertTrue(res.success)

    def test_flow_timeout(self):
        flow = BaseFlow()
        flow.timeout_seconds = 0.02

        def slow_step(ctx):
            time.sleep(0.05)
            return "done"

        flow.register_step("s1", slow_step)
        flow.register_step("s2", lambda ctx: "unreachable")

        res = flow.execute()
        self.assertEqual(res.status, FlowStatus.TIMEOUT)
        self.assertIn("flow timeout", res.error)

    def test_metrics_and_reset(self):
        flow = SimpleLinearFlow()
        flow.execute(input_data={"value": 1})
        flow.execute(input_data={"value": 2})

        metrics = flow.get_metrics()
        self.assertEqual(metrics["execution_count"], 2)
        self.assertEqual(metrics["success_count"], 2)
        self.assertEqual(metrics["failure_count"], 0)
        self.assertEqual(metrics["success_rate"], 1.0)
        self.assertGreaterEqual(metrics["total_latency_ms"], 0.0)

        flow.reset_metrics()
        reset_m = flow.get_metrics()
        self.assertEqual(reset_m["execution_count"], 0)
        self.assertEqual(reset_m["total_latency_ms"], 0.0)


class TestBaseFlowAsyncExecution(unittest.IsolatedAsyncioTestCase):
    async def test_async_linear_flow(self):
        flow = AsyncLinearFlow()
        res = await flow.execute_async(input_data={"user_id": "usr_42"})

        self.assertEqual(res.status, FlowStatus.COMPLETED)
        self.assertTrue(res.success)
        self.assertEqual(res.data["profile"], {"user_id": "usr_42", "name": "Alice", "vip": True})
        self.assertEqual(len(res.step_results), 2)

    async def test_async_hooks(self):
        flow = BaseFlow()
        events = []

        async def async_pre(ctx):
            await asyncio.sleep(0.005)
            events.append("async_pre")

        async def async_post(ctx, res):
            await asyncio.sleep(0.005)
            events.append("async_post")

        flow.add_pre_hook(async_pre)
        flow.add_post_hook(async_post)
        flow.register_step("s1", lambda ctx: "val")

        res = await flow.execute_async()
        self.assertTrue(res.success)
        self.assertEqual(events, ["async_pre", "async_post"])

    async def test_async_retry_and_timeout(self):
        flow = BaseFlow()
        flow.timeout_seconds = 0.03
        flow.retry_delay_seconds = 0.01

        async def slow_async_step(ctx):
            await asyncio.sleep(0.05)
            return "async_done"

        flow.register_step("slow_step", slow_async_step)
        flow.register_step("next_step", lambda ctx: "after")

        res = await flow.execute_async()
        self.assertEqual(res.status, FlowStatus.TIMEOUT)
        self.assertIn("flow timeout", res.error)


if __name__ == "__main__":
    unittest.main()
