"""Prompt 基类与管理器单元测试."""
import unittest
from typing import ClassVar

from react_ai.prompts.base import BasePrompt, PromptMessage, PromptResult
from react_ai.prompts.manager import PromptManager
from react_ai.prompts.templates.react import ReActPrompt
from react_ai.schemas.messages import ChatMessage, MessageRole


class AfterSalesRouterPrompt(BasePrompt):
    name = "after_sales_router"
    version = "1.0.1"
    description = "售后意图分类路由器"
    tags: ClassVar[list[str]] = ["router", "after_sales"]
    output_format = "json"
    output_schema: ClassVar[dict] = {
        "type": "object",
        "properties": {
            "intent": {"type": "string"},
            "confidence": {"type": "number"},
        },
        "required": ["intent", "confidence"],
    }
    required_variables: ClassVar[list[str]] = ["order_id", "user_question"]
    optional_variables: ClassVar[list[str]] = ["chat_history"]

    @property
    def system_template(self) -> str:
        return (
            "你是一个电商售后意图分类助手。当前订单号: {{order_id}}。\n"
            "请严格以 JSON 格式输出分类结果。"
        )

    @property
    def user_template(self) -> str:
        return "用户问题: {{user_question}}"

    @property
    def examples(self) -> list[dict]:
        return [
            {
                "input": "衣服尺码小了想换一件",
                "output": '{"intent": "exchange_goods", "confidence": 0.98}',
            },
            {
                "input": "水果烂了要求退款",
                "output": '{"intent": "refund_only", "confidence": 0.95}',
            },
        ]


class TestBasePrompt(unittest.TestCase):
    def test_prompt_message_and_conversion(self):
        msg = PromptMessage(role="system", content="Hello System")
        self.assertEqual(msg.role, "system")
        self.assertEqual(msg.content, "Hello System")

        chat_msg = msg.to_chat_message()
        self.assertIsInstance(chat_msg, ChatMessage)
        self.assertEqual(chat_msg.role, MessageRole.SYSTEM)
        self.assertEqual(chat_msg.content, "Hello System")

    def test_render_success(self):
        prompt = AfterSalesRouterPrompt()
        variables = {
            "order_id": "ORD_123456",
            "user_question": "我的快递怎么还没到？",
        }
        custom_trace = "trace-test-123"
        result = prompt.render(variables=variables, trace_id=custom_trace)

        self.assertIsInstance(result, PromptResult)
        self.assertEqual(result.prompt_name, "after_sales_router")
        self.assertEqual(result.version, "1.0.1")
        self.assertEqual(result.trace_id, custom_trace)
        self.assertEqual(result.variables_used["order_id"], "ORD_123456")
        self.assertGreaterEqual(result.render_duration_ms, 0)

        # 检查消息序列: 1(system) + 2*2(examples: user+assistant) + 1(user) = 6条
        self.assertEqual(len(result.messages), 6)

        # 1. System 消息
        self.assertEqual(result.messages[0].role, "system")
        self.assertIn("当前订单号: ORD_123456", result.messages[0].content)

        # 2. Example 1
        self.assertEqual(result.messages[1].role, "user")
        self.assertEqual(result.messages[1].content, "衣服尺码小了想换一件")
        self.assertEqual(result.messages[2].role, "assistant")
        self.assertIn("exchange_goods", result.messages[2].content)

        # 3. Example 2
        self.assertEqual(result.messages[3].role, "user")
        self.assertEqual(result.messages[3].content, "水果烂了要求退款")
        self.assertEqual(result.messages[4].role, "assistant")
        self.assertIn("refund_only", result.messages[4].content)

        # 4. User 消息
        self.assertEqual(result.messages[5].role, "user")
        self.assertEqual(result.messages[5].content, "用户问题: 我的快递怎么还没到？")

        # 转换为 ChatMessage 列表
        chat_messages = result.to_chat_messages()
        self.assertEqual(len(chat_messages), 6)
        self.assertEqual(chat_messages[0].role, MessageRole.SYSTEM)
        self.assertEqual(chat_messages[5].role, MessageRole.USER)

    def test_missing_required_variables(self):
        prompt = AfterSalesRouterPrompt()
        # 缺失 user_question
        variables = {"order_id": "ORD_001"}
        with self.assertRaises(ValueError) as ctx:
            prompt.render(variables)
        self.assertIn("缺少必填变量", str(ctx.exception))
        self.assertIn("user_question", str(ctx.exception))

        # 变量为 None 时也应视作缺失
        variables_with_none = {"order_id": "ORD_001", "user_question": None}
        with self.assertRaises(ValueError) as ctx2:
            prompt.render(variables_with_none)
        self.assertIn("user_question", str(ctx2.exception))

    def test_default_user_template_and_auto_trace(self):
        class MinimalPrompt(BasePrompt):
            name = "minimal_prompt"

            @property
            def system_template(self) -> str:
                return "You are a helpful assistant."

        prompt = MinimalPrompt()
        result = prompt.render(variables={"user_input": "你好"})
        self.assertTrue(len(result.trace_id) > 0)
        self.assertEqual(len(result.messages), 2)
        self.assertEqual(result.messages[0].role, "system")
        self.assertEqual(result.messages[0].content, "You are a helpful assistant.")
        self.assertEqual(result.messages[1].role, "user")
        self.assertEqual(result.messages[1].content, "你好")

    def test_react_prompt(self):
        react = ReActPrompt()
        res = react.render(variables={"user_input": "帮我查询天气"})
        self.assertEqual(res.prompt_name, "react_standard")
        self.assertEqual(len(res.messages), 2)
        self.assertIn("ReAct", res.messages[0].content)
        self.assertEqual(res.messages[1].content, "帮我查询天气")

    def test_prompt_manager(self):
        manager = PromptManager()
        custom_prompt = AfterSalesRouterPrompt()
        manager.register_prompt(custom_prompt)

        # 获取 Prompt
        fetched = manager.get_prompt("after_sales_router")
        self.assertIsNotNone(fetched)
        self.assertEqual(fetched.name, "after_sales_router")

        # 渲染 Prompt
        res = manager.render_prompt(
            "after_sales_router",
            variables={"order_id": "ORD_999", "user_question": "退货规则是什么？"}
        )
        self.assertEqual(res.prompt_name, "after_sales_router")
        self.assertIn("ORD_999", res.messages[0].content)

        # 未知 Prompt 抛出 KeyError
        with self.assertRaises(KeyError):
            manager.render_prompt("unknown_prompt", {})


if __name__ == "__main__":
    unittest.main()
