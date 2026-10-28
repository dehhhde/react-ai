"""工具基类与契约单元测试."""
import unittest
import asyncio
from typing import Any
from react_ai.tools.base import BaseTool, ToolContext, ToolResult, FunctionTool, tool
from react_ai.tools.registry import ToolRegistry
from react_ai.tools.builtin.bash import SafeBashTool
from react_ai.tools.builtin.retrieval import KnowledgeRetrievalTool


class DummyTool(BaseTool):
    name = "dummy_tool"
    description = "用于测试的虚拟工具"
    side_effects = "none"
    tags = ["test"]

    @property
    def input_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "text": {"type": "string"},
                "count": {"type": "integer", "minimum": 1, "maximum": 5},
                "status": {"type": "string", "enum": ["active", "inactive"]},
                "score": {"type": "number"},
                "flag": {"type": "boolean"},
            },
            "required": ["text", "count"],
            "additionalProperties": False,
        }

    def execute(self, arguments: dict, ctx: ToolContext) -> Any:
        if arguments["text"] == "raise_perm":
            raise PermissionError("无权限执行该操作")
        if arguments["text"] == "raise_val":
            raise ValueError("业务规则校验未通过")
        if arguments["text"] == "raise_exc":
            raise RuntimeError("系统不可用")
        return {"echo": arguments["text"], "count": arguments["count"]}


class WriteTool(BaseTool):
    name = "write_tool"
    description = "写操作工具"
    side_effects = "write"

    @property
    def input_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {"item": {"type": "string"}},
            "required": ["item"],
        }

    def execute(self, arguments: dict, ctx: ToolContext) -> Any:
        return f"saved {arguments['item']}"


class TestToolBase(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.ctx = ToolContext(
            caller_id="test_agent",
            tenant_id="tenant_001",
            user_id="user_123",
        )
        self.tool = DummyTool()

    def test_validation_required_missing(self):
        result = self.tool.call({"text": "hello"}, self.ctx)
        self.assertFalse(result.ok)
        self.assertEqual(result.error_type, "schema")
        self.assertEqual(result.error_code, "invalid_arguments")
        self.assertIn("required_field_missing", result.error_message)

    def test_validation_unknown_field(self):
        args = {"text": "hello", "count": 2, "unknown_arg": 123}
        result = self.tool.call(args, self.ctx)
        self.assertFalse(result.ok)
        self.assertEqual(result.error_code, "invalid_arguments")
        self.assertIn("unknown_field", result.error_message)

    def test_validation_type_mismatch(self):
        args = {"text": 12345, "count": 2}
        result = self.tool.call(args, self.ctx)
        self.assertFalse(result.ok)
        self.assertIn("type_mismatch", result.error_message)

    def test_validation_enum_and_range(self):
        args = {"text": "hello", "count": 10, "status": "invalid_status"}
        result = self.tool.call(args, self.ctx)
        self.assertFalse(result.ok)
        self.assertIn("above_maximum", result.error_message)
        self.assertIn("invalid_enum_value", result.error_message)

    def test_call_success(self):
        args = {"text": "hello", "count": 3, "status": "active", "score": 9.5, "flag": True}
        result = self.tool.call(args, self.ctx)
        self.assertTrue(result.ok)
        self.assertEqual(result.data["echo"], "hello")
        self.assertEqual(result.data["count"], 3)
        self.assertEqual(result.tool_name, "dummy_tool")
        self.assertEqual(result.trace_id, self.ctx.trace_id)

    def test_idempotency_key_requirement(self):
        write_tool = WriteTool()
        # 无幂等键时应当被拒绝
        res_fail = write_tool.call({"item": "book"}, self.ctx)
        self.assertFalse(res_fail.ok)
        self.assertEqual(res_fail.error_code, "missing_idempotency_key")

        # 携带幂等键时正常执行
        ctx_with_key = ToolContext(
            caller_id="agent",
            tenant_id="tenant_1",
            user_id="user_1",
            idempotency_key="idemp_999",
        )
        res_ok = write_tool.call({"item": "book"}, ctx_with_key)
        self.assertTrue(res_ok.ok)
        self.assertEqual(res_ok.data, "saved book")

    def test_exception_wrapping(self):
        res_perm = self.tool.call({"text": "raise_perm", "count": 1}, self.ctx)
        self.assertFalse(res_perm.ok)
        self.assertEqual(res_perm.error_type, "auth")
        self.assertEqual(res_perm.error_code, "permission_denied")

        res_val = self.tool.call({"text": "raise_val", "count": 1}, self.ctx)
        self.assertFalse(res_val.ok)
        self.assertEqual(res_val.error_type, "business")
        self.assertEqual(res_val.error_code, "business_rejected")

        res_exc = self.tool.call({"text": "raise_exc", "count": 1}, self.ctx)
        self.assertFalse(res_exc.ok)
        self.assertEqual(res_exc.error_type, "external")
        self.assertEqual(res_exc.error_code, "tool_execution_failed")
        self.assertTrue(res_exc.retryable)

    async def test_acall_async(self):
        args = {"text": "async_call", "count": 2}
        result = await self.tool.acall(args, self.ctx)
        self.assertTrue(result.ok)
        self.assertEqual(result.data["echo"], "async_call")

    def test_get_definition(self):
        defn = self.tool.get_definition()
        self.assertEqual(defn.name, "dummy_tool")
        self.assertIn("text", defn.parameters.properties)
        self.assertIn("count", defn.parameters.required)

    def test_tool_decorator(self):
        @tool(name="multiply", description="乘法计算")
        def multiply(x: int, y: int) -> int:
            return x * y

        self.assertIsInstance(multiply, FunctionTool)
        self.assertEqual(multiply.name, "multiply")
        self.assertIn("x", multiply.input_schema["properties"])

        res = multiply.call({"x": 6, "y": 7}, self.ctx)
        self.assertTrue(res.ok)
        self.assertEqual(res.data, 42)

    async def test_registry_integration(self):
        registry = ToolRegistry()
        registry.register(self.tool)
        registry.register(KnowledgeRetrievalTool())

        defs = registry.list_definitions()
        self.assertEqual(len(defs), 2)

        data = await registry.execute("dummy_tool", {"text": "reg_test", "count": 1})
        self.assertEqual(data["echo"], "reg_test")

        raw_res = await registry.call("dummy_tool", {"text": "reg_test", "count": 1})
        self.assertIsInstance(raw_res, ToolResult)
        self.assertTrue(raw_res.ok)


if __name__ == "__main__":
    unittest.main()


class TestBuiltinTools(unittest.TestCase):
    def setUp(self):
        self.ctx = ToolContext(
            caller_id="agent",
            tenant_id="tenant",
            user_id="user",
            idempotency_key="idemp_1",
        )

    def test_bash_safe(self):
        bash = SafeBashTool()
        res = bash.call({"command": "echo test_bash"}, self.ctx)
        self.assertTrue(res.ok)
        self.assertIn("test_bash", res.data["stdout"])

    def test_bash_forbidden_intercepted(self):
        bash = SafeBashTool()
        res = bash.call({"command": "rm -rf /something"}, self.ctx)
        self.assertFalse(res.ok)
        self.assertEqual(res.error_type, "auth")
        self.assertEqual(res.error_code, "permission_denied")

    def test_retrieval(self):
        rag = KnowledgeRetrievalTool()
        res = rag.call({"query": "测试服务", "top_k": 2}, self.ctx)
        self.assertTrue(res.ok)
        self.assertEqual(len(res.data), 2)
        self.assertIn("测试服务", res.data[0]["content"])
