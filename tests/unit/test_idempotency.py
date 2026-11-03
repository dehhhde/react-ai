"""写操作幂等去重单元测试：同键重放、失败重试、执行中冲突、TTL 过期、引擎集成键派生."""
import unittest

from react_ai.core.context import RunContext
from react_ai.idempotency.guard import IdempotencyGuard, reset_idempotency_guard
from react_ai.idempotency.store import IdempotencyStatus, InMemoryIdempotencyStore
from react_ai.tools.base import BaseTool, ToolContext
from react_ai.tools.registry import ToolRegistry


class CountingWriteTool(BaseTool):
    name = "counting_write"
    description = "计数写工具（测试用）"
    side_effects = "write"

    def __init__(self):
        self.calls = 0

    @property
    def input_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {"order_id": {"type": "string"}},
            "required": ["order_id"],
        }

    def execute(self, arguments: dict, ctx) -> dict:
        self.calls += 1
        return {"ticket": f"TKT_{self.calls}", "order_id": arguments["order_id"]}


class FlakyWriteTool(BaseTool):
    name = "flaky_write"
    description = "可切换失败/成功的写工具（测试用）"
    side_effects = "destructive"

    def __init__(self):
        self.calls = 0
        self.should_fail = True

    @property
    def input_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {"order_id": {"type": "string"}},
            "required": ["order_id"],
        }

    def execute(self, arguments: dict, ctx) -> dict:
        self.calls += 1
        if self.should_fail:
            raise ValueError("库存不足，业务拒绝")
        return {"intercepted": True}


class ReadTool(BaseTool):
    name = "idempotent_read"
    description = "只读工具（测试用）"
    side_effects = "read"

    def __init__(self):
        self.calls = 0

    @property
    def input_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {"order_id": {"type": "string"}},
            "required": ["order_id"],
        }

    def execute(self, arguments: dict, ctx) -> dict:
        self.calls += 1
        return {"status": "DELIVERED"}


def make_ctx(key=None, tenant="t1") -> ToolContext:
    return ToolContext(
        caller_id="test", tenant_id=tenant, user_id="u1",
        trace_id="tr1", idempotency_key=key,
    )


class TestIdempotencyGuard(unittest.TestCase):
    def setUp(self):
        self.guard = reset_idempotency_guard(
            IdempotencyGuard(store=InMemoryIdempotencyStore(), ttl_seconds=3600)
        )

    def test_first_call_executes_second_replays(self):
        tool = CountingWriteTool()
        r1 = tool.call({"order_id": "O1"}, make_ctx("k1"))
        r2 = tool.call({"order_id": "O1"}, make_ctx("k1"))
        self.assertTrue(r1.ok and r2.ok)
        self.assertFalse(r1.replayed)
        self.assertTrue(r2.replayed)
        self.assertEqual(tool.calls, 1)
        self.assertEqual(r2.data, r1.data)

    def test_different_keys_both_execute(self):
        tool = CountingWriteTool()
        tool.call({"order_id": "O1"}, make_ctx("k1"))
        tool.call({"order_id": "O2"}, make_ctx("k2"))
        self.assertEqual(tool.calls, 2)

    def test_missing_key_still_rejected(self):
        tool = CountingWriteTool()
        result = tool.call({"order_id": "O1"}, make_ctx(None))
        self.assertFalse(result.ok)
        self.assertEqual(result.error_code, "missing_idempotency_key")
        self.assertEqual(tool.calls, 0)

    def test_failed_execution_allows_retry(self):
        tool = FlakyWriteTool()
        r1 = tool.call({"order_id": "O1"}, make_ctx("k1"))
        self.assertFalse(r1.ok)
        self.assertEqual(r1.error_type, "business")
        tool.should_fail = False
        r2 = tool.call({"order_id": "O1"}, make_ctx("k1"))
        self.assertTrue(r2.ok)
        self.assertFalse(r2.replayed)
        self.assertEqual(tool.calls, 2)

    def test_exception_allows_retry(self):
        tool = FlakyWriteTool()
        tool.call({"order_id": "O1"}, make_ctx("k1"))
        tool.should_fail = False
        r2 = tool.call({"order_id": "O1"}, make_ctx("k1"))
        self.assertTrue(r2.ok)

    def test_conflict_while_in_progress(self):
        tool = CountingWriteTool()
        # 模拟另一并发请求已占位（IN_PROGRESS）
        self.guard.begin(tool.name, "t1", "k9")
        result = tool.call({"order_id": "O1"}, make_ctx("k9"))
        self.assertFalse(result.ok)
        self.assertEqual(result.error_code, "idempotency_conflict")
        self.assertTrue(result.retryable)
        self.assertEqual(tool.calls, 0)

    def test_read_tool_not_guarded(self):
        tool = ReadTool()
        tool.call({"order_id": "O1"}, make_ctx(None))
        tool.call({"order_id": "O1"}, make_ctx(None))
        self.assertEqual(tool.calls, 2)

    def test_ttl_expiry_allows_reexecution(self):
        reset_idempotency_guard(
            IdempotencyGuard(store=InMemoryIdempotencyStore(), ttl_seconds=-1)
        )
        tool = CountingWriteTool()
        tool.call({"order_id": "O1"}, make_ctx("k1"))
        r2 = tool.call({"order_id": "O1"}, make_ctx("k1"))
        self.assertTrue(r2.ok)
        self.assertFalse(r2.replayed)
        self.assertEqual(tool.calls, 2)

    def test_record_status_transitions(self):
        tool = CountingWriteTool()
        tool.call({"order_id": "O1"}, make_ctx("k1"))
        record = self.guard.store.find("counting_write", "t1", "k1")
        self.assertEqual(record.status, IdempotencyStatus.SUCCEEDED)
        self.assertEqual(record.result.data["ticket"], "TKT_1")


class TestIdempotencyEngineIntegration(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        reset_idempotency_guard(
            IdempotencyGuard(store=InMemoryIdempotencyStore(), ttl_seconds=3600)
        )

    async def test_tool_call_id_auto_generates_key(self):
        """集成缺陷修复：RunContext 无 metadata 时，写工具以 tool_call_id 为键可正常执行."""
        registry = ToolRegistry()
        tool = CountingWriteTool()
        registry.register(tool)
        context = RunContext()

        data = await registry.execute(
            "counting_write", {"order_id": "O1"}, context, tool_call_id="call_1"
        )
        self.assertEqual(data["ticket"], "TKT_1")

        # 同 tool_call_id 重复执行（模型重复调用/断点恢复）→ 重放缓存，不重复写
        data2 = await registry.execute(
            "counting_write", {"order_id": "O1"}, context, tool_call_id="call_1"
        )
        self.assertEqual(data2, data)
        self.assertEqual(tool.calls, 1)

    async def test_metadata_key_takes_priority(self):
        registry = ToolRegistry()
        tool = CountingWriteTool()
        registry.register(tool)
        context = RunContext(metadata={"idempotency_key": "biz_key_1"})

        await registry.execute("counting_write", {"order_id": "O1"}, context, tool_call_id="call_a")
        await registry.execute("counting_write", {"order_id": "O1"}, context, tool_call_id="call_b")
        # 业务级键优先于调用级 id：跨 tool_call 仍去重
        self.assertEqual(tool.calls, 1)

    async def test_registry_call_returns_replayed_flag(self):
        registry = ToolRegistry()
        tool = CountingWriteTool()
        registry.register(tool)
        r1 = await registry.call("counting_write", {"order_id": "O1"}, RunContext(), tool_call_id="call_1")
        r2 = await registry.call("counting_write", {"order_id": "O1"}, RunContext(), tool_call_id="call_1")
        self.assertTrue(r1.ok and r2.ok)
        self.assertFalse(r1.replayed)
        self.assertTrue(r2.replayed)
        self.assertEqual(tool.calls, 1)


if __name__ == "__main__":
    unittest.main()
