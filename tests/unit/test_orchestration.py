"""BaseOrchestrator 多智能体编排器基类、上下文、结果契约与并发策略单元测试."""
import asyncio
import time
import unittest
from typing import Any

from react_ai.orchestration.base import (
    AgentContext,
    AgentResult,
    AgentStatus,
    BaseAgent,
    BaseOrchestrator,
    OrchestratorContext,
    OrchestratorResult,
    OrchestratorStatus,
    ParallelOrchestrator,
    SequentialOrchestrator,
)


class DummyAgent(BaseAgent):
    """测试用简单同步 Agent."""

    name = "dummy_agent"

    def __init__(self, agent_id: str, multiplier: int = 2):
        super().__init__(agent_id=agent_id)
        self.multiplier = multiplier

    def execute(self, context: AgentContext) -> Any:
        val = context.input_data.get("val", 1)
        res = val * self.multiplier
        context.output_data["computed"] = res
        return {"result": res}


class AsyncDummyAgent(BaseAgent):
    """测试用异步 Agent."""

    name = "async_dummy_agent"

    def __init__(self, agent_id: str, delay: float = 0.01):
        super().__init__(agent_id=agent_id)
        self.delay = delay

    def execute(self, context: AgentContext) -> Any:
        time.sleep(self.delay)
        return {"sync_done": True}

    async def execute_async(self, context: AgentContext) -> Any:
        await asyncio.sleep(self.delay)
        return {"async_done": True, "agent_id": self.agent_id}


class FlakyAgent(BaseAgent):
    """测试重试的 Flaky Agent."""

    name = "flaky_agent"

    def __init__(self, agent_id: str, fail_times: int = 1):
        super().__init__(agent_id=agent_id)
        self.fail_times = fail_times
        self.attempts = 0
        self.max_retries = 3
        self.retry_delay_seconds = 0.01

    def execute(self, context: AgentContext) -> Any:
        self.attempts += 1
        if self.attempts <= self.fail_times:
            raise RuntimeError(f"fail attempt {self.attempts}")
        return {"recovered": True, "attempts": self.attempts}

    async def execute_async(self, context: AgentContext) -> Any:
        self.attempts += 1
        if self.attempts <= self.fail_times:
            raise RuntimeError(f"fail attempt async {self.attempts}")
        return {"recovered_async": True, "attempts": self.attempts}


class RunProtocolAgent:
    """具备 run 方法但未继承 BaseAgent 的兼容对象."""

    def __init__(self, agent_id: str = "run_agent"):
        self.agent_id = agent_id
        self.name = "run_protocol_agent"

    def run(self, input_data: dict[str, Any]) -> dict[str, Any]:
        return {"echo": input_data.get("msg", "none")}


class TestOrchestratorDataContracts(unittest.TestCase):
    """测试编排状态枚举与数据契约模型."""

    def test_enums(self):
        self.assertEqual(OrchestratorStatus.PENDING.value, "pending")
        self.assertEqual(OrchestratorStatus.RUNNING.value, "running")
        self.assertEqual(OrchestratorStatus.COMPLETED.value, "completed")
        self.assertEqual(OrchestratorStatus.FAILED.value, "failed")
        self.assertEqual(OrchestratorStatus.CANCELLED.value, "cancelled")
        self.assertEqual(OrchestratorStatus.TIMEOUT.value, "timeout")
        self.assertEqual(OrchestratorStatus.PARTIAL.value, "partial")

        self.assertEqual(AgentStatus.IDLE.value, "idle")
        self.assertEqual(AgentStatus.RUNNING.value, "running")
        self.assertEqual(AgentStatus.COMPLETED.value, "completed")
        self.assertEqual(AgentStatus.FAILED.value, "failed")
        self.assertEqual(AgentStatus.TIMEOUT.value, "timeout")

    def test_agent_context_and_result(self):
        ctx = AgentContext(agent_id="a1", agent_name="agent_one")
        self.assertEqual(ctx.get_elapsed_ms(), 0.0)
        ctx.start_time = time.time() - 0.05
        self.assertGreater(ctx.get_elapsed_ms(), 30.0)

        ar = AgentResult(
            agent_id="a1",
            agent_name="agent_one",
            success=True,
            data={"k": "v"},
            latency_ms=12.345,
        )
        d = ar.to_dict()
        self.assertEqual(d["agent_id"], "a1")
        self.assertEqual(d["success"], True)
        self.assertEqual(d["latency_ms"], 12.35)

    def test_orchestrator_context_and_result(self):
        ctx = OrchestratorContext(request_id="req-123", input_data={"x": 10})
        self.assertTrue(len(ctx.orchestration_id) > 0)
        self.assertEqual(ctx.request_id, "req-123")
        self.assertEqual(ctx.get_elapsed_ms(), 0.0)

        res = OrchestratorResult(
            orchestration_id="orch-1",
            status=OrchestratorStatus.COMPLETED,
            success=True,
            data={"result": 100},
            total_latency_ms=45.678,
        )
        d = res.to_dict()
        self.assertEqual(d["orchestration_id"], "orch-1")
        self.assertEqual(d["status"], "completed")
        self.assertEqual(d["success"], True)
        self.assertEqual(d["total_latency_ms"], 45.68)


class TestBaseOrchestratorManagement(unittest.TestCase):
    """测试 Agent 注册注销与钩子机制."""

    def test_agent_registration(self):
        orch = SequentialOrchestrator()
        a1 = DummyAgent("agent_1")
        a2 = DummyAgent("agent_2")
        a3 = DummyAgent("agent_3")

        orch.register_agent(a1)
        orch.register_agent(a2)
        orch.register_agent(a3, order=0)  # 插入最前面

        agents = orch.list_agents()
        self.assertEqual([a.agent_id for a in agents], ["agent_3", "agent_1", "agent_2"])
        self.assertEqual(orch.get_agent("agent_1"), a1)

        unreg = orch.unregister_agent("agent_1")
        self.assertTrue(unreg)
        self.assertEqual([a.agent_id for a in orch.list_agents()], ["agent_3", "agent_2"])
        self.assertIsNone(orch.get_agent("agent_1"))

    def test_disabled_orchestrator(self):
        orch = SequentialOrchestrator()
        orch.enabled = False
        res = orch.execute(input_data={"test": 1})
        self.assertEqual(res.status, OrchestratorStatus.FAILED)
        self.assertIn("disabled", res.error)
        self.assertFalse(res.success)

    def test_hooks_execution(self):
        orch = SequentialOrchestrator()
        a1 = DummyAgent("a1")
        orch.register_agent(a1)

        events = []
        orch.add_pre_hook(lambda ctx: events.append("pre_orch"))
        orch.add_post_hook(lambda ctx, res: events.append("post_orch"))
        orch.add_agent_pre_hook(lambda aid, ctx: events.append(f"pre_agent_{aid}"))
        orch.add_agent_post_hook(lambda aid, ar, ctx: events.append(f"post_agent_{aid}"))

        res = orch.execute(input_data={"val": 5})
        self.assertTrue(res.success)
        self.assertEqual(
            events,
            ["pre_orch", "pre_agent_a1", "post_agent_a1", "post_orch"],
        )


class TestSyncExecution(unittest.TestCase):
    """测试编排器的同步执行能力."""

    def test_sequential_success(self):
        orch = SequentialOrchestrator()
        a1 = DummyAgent("a1", multiplier=2)
        a2 = DummyAgent("a2", multiplier=3)
        orch.register_agent(a1)
        orch.register_agent(a2)

        res = orch(input_data={"val": 10})
        self.assertEqual(res.status, OrchestratorStatus.COMPLETED)
        self.assertTrue(res.success)
        self.assertEqual(len(res.agent_results), 2)
        self.assertEqual(res.agent_results[0].data, {"result": 20})
        self.assertEqual(res.agent_results[1].data, {"result": 30})

    def test_retry_success(self):
        orch = SequentialOrchestrator()
        flaky = FlakyAgent("flaky", fail_times=2)
        orch.register_agent(flaky)

        res = orch.execute()
        self.assertEqual(res.status, OrchestratorStatus.COMPLETED)
        self.assertTrue(res.success)
        self.assertEqual(flaky.attempts, 3)

    def test_fail_fast_enabled(self):
        orch = SequentialOrchestrator()
        orch.fail_fast = True
        flaky = FlakyAgent("flaky", fail_times=10)
        a2 = DummyAgent("a2")
        orch.register_agent(flaky)
        orch.register_agent(a2)

        res = orch.execute()
        self.assertEqual(res.status, OrchestratorStatus.FAILED)
        self.assertFalse(res.success)
        self.assertEqual(len(res.agent_results), 1)
        self.assertIn("flaky failed", res.error)

    def test_fail_fast_disabled_partial_success(self):
        orch = SequentialOrchestrator()
        orch.fail_fast = False
        flaky = FlakyAgent("flaky", fail_times=10)
        a2 = DummyAgent("a2")
        orch.register_agent(flaky)
        orch.register_agent(a2)

        res = orch.execute(input_data={"val": 4})
        self.assertEqual(res.status, OrchestratorStatus.PARTIAL)
        self.assertFalse(res.success)
        self.assertEqual(len(res.agent_results), 2)
        self.assertFalse(res.agent_results[0].success)
        self.assertTrue(res.agent_results[1].success)

    def test_multi_protocol_compatibility(self):
        orch = SequentialOrchestrator()
        p_agent = RunProtocolAgent("run_ag")
        callable_agent = lambda ctx: {"lambda_ok": True}

        orch.register_agent(p_agent)
        orch.register_agent(callable_agent)

        res = orch.execute(input_data={"msg": "hello_orchestrator"})
        self.assertEqual(res.status, OrchestratorStatus.COMPLETED)
        self.assertTrue(res.success)
        self.assertEqual(len(res.agent_results), 2)
        self.assertEqual(res.agent_results[0].data, {"echo": "hello_orchestrator"})
        self.assertEqual(res.agent_results[1].data, {"lambda_ok": True})

    def test_timeout(self):
        orch = SequentialOrchestrator()
        orch.timeout_seconds = 0.05
        # 慢速 Agent
        orch.register_agent(lambda ctx: time.sleep(0.08) or "done1")
        orch.register_agent(DummyAgent("a2"))

        res = orch.execute()
        self.assertEqual(res.status, OrchestratorStatus.TIMEOUT)
        self.assertIn("timeout", res.error)


class TestAsyncExecution(unittest.IsolatedAsyncioTestCase):
    """测试编排器的异步执行能力."""

    async def test_async_success(self):
        orch = SequentialOrchestrator()
        a1 = AsyncDummyAgent("async_1", delay=0.01)
        a2 = AsyncDummyAgent("async_2", delay=0.01)
        orch.register_agent(a1)
        orch.register_agent(a2)

        res = await orch.execute_async()
        self.assertEqual(res.status, OrchestratorStatus.COMPLETED)
        self.assertTrue(res.success)
        self.assertEqual(len(res.agent_results), 2)

    async def test_async_retry(self):
        orch = SequentialOrchestrator()
        flaky = FlakyAgent("async_flaky", fail_times=2)
        orch.register_agent(flaky)

        res = await orch.execute_async()
        self.assertEqual(res.status, OrchestratorStatus.COMPLETED)
        self.assertTrue(res.success)
        self.assertEqual(flaky.attempts, 3)

    async def test_async_hooks(self):
        orch = SequentialOrchestrator()
        a1 = AsyncDummyAgent("a1", delay=0.01)
        orch.register_agent(a1)

        events = []

        async def pre_hook(ctx):
            await asyncio.sleep(0.001)
            events.append("pre_async")

        def post_hook(ctx, res):
            events.append("post_sync")

        orch.add_pre_hook(pre_hook)
        orch.add_post_hook(post_hook)

        res = await orch.execute_async()
        self.assertTrue(res.success)
        self.assertEqual(events, ["pre_async", "post_sync"])


class TestParallelOrchestrator(unittest.IsolatedAsyncioTestCase):
    """测试并发并行编排器."""

    def test_parallel_sync_success(self):
        orch = ParallelOrchestrator()
        a1 = DummyAgent("a1", multiplier=2)
        a2 = DummyAgent("a2", multiplier=5)
        a3 = DummyAgent("a3", multiplier=10)
        orch.register_agent(a1)
        orch.register_agent(a2)
        orch.register_agent(a3)

        res = orch.execute(input_data={"val": 3})
        self.assertEqual(res.status, OrchestratorStatus.COMPLETED)
        self.assertTrue(res.success)
        self.assertEqual(len(res.agent_results), 3)
        # 结果顺序应与注册顺序一致
        self.assertEqual(res.agent_results[0].agent_id, "a1")
        self.assertEqual(res.agent_results[1].agent_id, "a2")
        self.assertEqual(res.agent_results[2].agent_id, "a3")

    async def test_parallel_async_success(self):
        orch = ParallelOrchestrator()
        a1 = AsyncDummyAgent("a1", delay=0.02)
        a2 = AsyncDummyAgent("a2", delay=0.02)
        a3 = AsyncDummyAgent("a3", delay=0.02)
        orch.register_agent(a1)
        orch.register_agent(a2)
        orch.register_agent(a3)

        start = time.time()
        res = await orch.execute_async()
        duration = time.time() - start

        self.assertEqual(res.status, OrchestratorStatus.COMPLETED)
        self.assertTrue(res.success)
        self.assertEqual(len(res.agent_results), 3)
        self.assertLess(duration, 1.0)

    async def test_parallel_async_fail_fast(self):
        orch = ParallelOrchestrator()
        orch.fail_fast = True
        flaky = FlakyAgent("flaky", fail_times=10)
        a2 = AsyncDummyAgent("a2", delay=0.02)
        orch.register_agent(flaky)
        orch.register_agent(a2)

        res = await orch.execute_async()
        self.assertEqual(res.status, OrchestratorStatus.FAILED)
        self.assertFalse(res.success)


class TestMetricsAndReset(unittest.TestCase):
    """测试度量指标与重置逻辑."""

    def test_metrics_and_reset(self):
        orch = SequentialOrchestrator()
        a1 = DummyAgent("a1")
        orch.register_agent(a1)

        res = orch.execute()
        self.assertTrue(res.success)

        stats = orch.get_stats()
        self.assertEqual(stats["execution_count"], 1)
        self.assertEqual(stats["success_count"], 1)
        self.assertEqual(stats["failure_count"], 0)

        metrics = orch.get_metrics()
        self.assertEqual(metrics["orchestrator_name"], "sequential_orchestrator")

        orch.reset_stats()
        self.assertEqual(orch.get_stats()["execution_count"], 0)

        orch.reset()
        self.assertEqual(len(orch.list_agents()), 0)


if __name__ == "__main__":
    unittest.main()
