"""电商售后领域 9 大工具单元测试."""
import unittest
from decimal import Decimal
from react_ai.tools.base import ToolContext, ToolResult
from react_ai.tools.registry import ToolRegistry
from react_ai.tools.aftersales import (
    QueryOrderTool,
    QueryLogisticsTool,
    SearchPolicyTool,
    QueryUserHistoryTool,
    CreateRefundTool,
    CreateExchangeOrderTool,
    InterceptShipmentTool,
    SendNotificationTool,
    EscalateToHumanTool,
)


class TestAfterSalesTools(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.read_ctx = ToolContext(
            caller_id="agent_service",
            tenant_id="tenant_001",
            user_id="USR_001",
        )
        self.write_ctx = ToolContext(
            caller_id="agent_service",
            tenant_id="tenant_001",
            user_id="USR_001",
            idempotency_key="idemp_test_key_001",
        )
        self._reset_db_state()

    def _reset_db_state(self):
        import psycopg
        from react_ai.config.settings import settings
        with psycopg.connect(settings.database.url, autocommit=True) as conn:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM notifications")
                cur.execute("DELETE FROM after_sales_tickets")
                cur.execute("UPDATE order_items SET after_sales_status = 'NONE'")
                cur.execute(
                    "UPDATE logistics SET status = 'IN_TRANSIT', signed_time = NULL, current_location = '在途运输' WHERE tracking_number = 'SF60060006'"
                )

    def test_tool_definitions_and_metadata(self):
        tools = [
            QueryOrderTool(),
            QueryLogisticsTool(),
            SearchPolicyTool(),
            QueryUserHistoryTool(),
            CreateRefundTool(),
            CreateExchangeOrderTool(),
            InterceptShipmentTool(),
            SendNotificationTool(),
            EscalateToHumanTool(),
        ]
        self.assertEqual(len(tools), 9)

        read_names = {"query_order", "query_logistics", "search_policy", "query_user_history"}
        write_names = {"create_refund", "create_exchange_order", "intercept_shipment", "send_notification", "escalate_to_human"}

        for t in tools:
            defn = t.get_definition()
            self.assertEqual(defn.name, t.name)
            self.assertTrue(len(defn.description) > 0)
            if t.name in read_names:
                self.assertEqual(t.side_effects, "read")
            elif t.name in write_names:
                self.assertEqual(t.side_effects, "write")
                self.assertTrue(defn.requires_approval)

    def test_query_order_by_order_id(self):
        tool = QueryOrderTool()
        res = tool.call({"order_id": "ORD_CLOTH_001"}, self.read_ctx)
        self.assertTrue(res.ok)
        self.assertEqual(res.data["order_id"], "ORD_CLOTH_001")
        self.assertEqual(res.data["user_id"], "USR_001")
        self.assertEqual(res.data["order_status"], "DELIVERED")
        self.assertTrue(len(res.data["items"]) >= 1)
        self.assertEqual(res.data["items"][0]["item_id"], "ITEM_CLOTH_001")

    def test_query_order_by_user_id(self):
        tool = QueryOrderTool()
        res = tool.call({"user_id": "USR_001"}, self.read_ctx)
        self.assertTrue(res.ok)
        self.assertIn("orders", res.data)
        self.assertTrue(res.data["total_count"] >= 2)

    def test_query_order_validation_errors(self):
        tool = QueryOrderTool()
        res_missing = tool.call({}, self.read_ctx)
        self.assertFalse(res_missing.ok)
        self.assertEqual(res_missing.error_type, "business")

        res_not_found = tool.call({"order_id": "NON_EXIST_999"}, self.read_ctx)
        self.assertFalse(res_not_found.ok)
        self.assertIn("未找到订单", res_not_found.error_message)

    def test_query_logistics(self):
        tool = QueryLogisticsTool()
        # 在途包裹，支持拦截
        res_transit = tool.call({"order_id": "ORD_TRANSIT_006"}, self.read_ctx)
        self.assertTrue(res_transit.ok)
        self.assertEqual(res_transit.data["status"], "IN_TRANSIT")
        self.assertTrue(res_transit.data["is_shipped"])
        self.assertTrue(res_transit.data["can_intercept"])

        # 已签收包裹，不可拦截
        res_signed = tool.call({"tracking_number": "SF10010001"}, self.read_ctx)
        self.assertTrue(res_signed.ok)
        self.assertEqual(res_signed.data["status"], "DELIVERED")
        self.assertFalse(res_signed.data["can_intercept"])

    def test_search_policy(self):
        tool = SearchPolicyTool()
        # 直接查询品类政策
        res_cat = tool.call({"category": "CLOTHING"}, self.read_ctx)
        self.assertTrue(res_cat.ok)
        self.assertEqual(res_cat.data["category"], "CLOTHING")
        self.assertEqual(res_cat.data["support_no_reason_days"], 7)

        # 关联订单查询（在有效期内）
        res_valid = tool.call({"category": "CLOTHING", "order_id": "ORD_CLOTH_001"}, self.read_ctx)
        self.assertTrue(res_valid.ok)
        self.assertTrue(res_valid.data["within_return_window"])
        self.assertTrue(res_valid.data["eligible_for_return"])

        # 关联已过期订单 (ORD_EXPIRED_004 签收已满40天)
        res_expired = tool.call({"category": "CLOTHING", "order_id": "ORD_EXPIRED_004"}, self.read_ctx)
        self.assertTrue(res_expired.ok)
        self.assertFalse(res_expired.data["within_return_window"])
        self.assertFalse(res_expired.data["eligible_for_return"])

        # 生鲜无理由退货检查 (FRESH_FOOD support_no_reason_days=0)
        res_fresh = tool.call({"category": "FRESH_FOOD", "reason_type": "no_reason"}, self.read_ctx)
        self.assertTrue(res_fresh.ok)
        self.assertFalse(res_fresh.data["eligible_for_return"])

    def test_query_user_history(self):
        tool = QueryUserHistoryTool()
        res = tool.call({"user_id": "USR_001", "time_range_days": 90}, self.read_ctx)
        self.assertTrue(res.ok)
        self.assertEqual(res.data["user_id"], "USR_001")
        self.assertTrue(res.data["total_orders"] >= 2)
        self.assertIn("risk_level", res.data)
        self.assertIn("is_high_risk", res.data)


    def test_create_refund_idempotency_requirement(self):
        tool = CreateRefundTool()
        args = {
            "order_id": "ORD_CLOTH_001",
            "item_id": "ITEM_CLOTH_001",
            "refund_amount": 50.0,
            "reason": "商品有微小瑕疵",
        }
        # 无幂等键必被拦截
        res_fail = tool.call(args, self.read_ctx)
        self.assertFalse(res_fail.ok)
        self.assertEqual(res_fail.error_code, "missing_idempotency_key")

    def test_create_refund_amount_limit_and_success(self):
        tool = CreateRefundTool()
        # 1. 超过单品总金额被业务拦截 (单价199)
        args_over = {
            "order_id": "ORD_CLOTH_001",
            "item_id": "ITEM_CLOTH_001",
            "refund_amount": 999.0,
            "reason": "超额退款",
        }
        res_over = tool.call(args_over, self.write_ctx)
        self.assertFalse(res_over.ok)
        self.assertEqual(res_over.error_type, "business")
        self.assertIn("超过该商品总金额", res_over.error_message)

        # 2. 正常发起退款 (在限额内自动审核)
        args_ok = {
            "order_id": "ORD_CLOTH_001",
            "item_id": "ITEM_CLOTH_001",
            "refund_amount": 199.0,
            "reason": "尺码不合",
            "refund_type": "RETURN_AND_REFUND",
        }
        res_ok = tool.call(args_ok, self.write_ctx)
        self.assertTrue(res_ok.ok)
        self.assertTrue(res_ok.data["ticket_id"].startswith("TKT_REF_"))
        self.assertEqual(res_ok.data["status"], "NEED_RETURN_SHIPPING")

        # 3. 奢侈品订单发起退款 (max_auto_refund_amount = 0，必须进入 PENDING_REVIEW)
        args_lux = {
            "order_id": "ORD_LUXURY_005",
            "item_id": "ITEM_LUXURY_005",
            "refund_amount": 8800.0,
            "reason": "高价值物品退款申请",
        }
        res_lux = tool.call(args_lux, self.write_ctx)
        self.assertTrue(res_lux.ok)
        self.assertFalse(res_lux.data["auto_approved"])
        self.assertEqual(res_lux.data["status"], "PENDING_REVIEW")

    def test_create_exchange_order(self):
        tool = CreateExchangeOrderTool()
        # 1. 缺货SKU拦截
        args_oos = {
            "order_id": "ORD_CLOTH_002",
            "item_id": "ITEM_CLOTH_002",
            "target_sku_id": "SKU_1002_OOS",
            "reason": "换货缺货",
        }
        res_oos = tool.call(args_oos, self.write_ctx)
        self.assertFalse(res_oos.ok)
        self.assertEqual(res_oos.error_type, "business")
        self.assertIn("库存不足", res_oos.error_message)

        # 2. 正常换货单创建
        args_ok = {
            "order_id": "ORD_CLOTH_002",
            "item_id": "ITEM_CLOTH_002",
            "target_sku_id": "SKU_1002_41",
            "reason": "鞋码偏大，换41码",
        }
        res_ok = tool.call(args_ok, self.write_ctx)
        self.assertTrue(res_ok.ok)
        self.assertTrue(res_ok.data["ticket_id"].startswith("TKT_EXC_"))
        self.assertEqual(res_ok.data["target_sku_id"], "SKU_1002_41")

    def test_intercept_shipment(self):
        tool = InterceptShipmentTool()
        # 1. 尝试拦截已签收订单 (SF10010001 已签收，必须拒绝)
        res_delivered = tool.call(
            {"tracking_number": "SF10010001", "reason": "已签收但尝试拦截"},
            self.write_ctx,
        )
        self.assertFalse(res_delivered.ok)
        self.assertEqual(res_delivered.error_type, "business")
        self.assertIn("已由用户签收", res_delivered.error_message)

        # 2. 拦截在途包裹 (ORD_TRANSIT_006)
        res_transit = tool.call(
            {"order_id": "ORD_TRANSIT_006", "reason": "用户下错地址"},
            self.write_ctx,
        )
        self.assertTrue(res_transit.ok)
        self.assertEqual(res_transit.data["current_status"], "EXCEPTION")
        self.assertTrue(res_transit.data["intercept_success"])

    def test_send_notification(self):
        tool = SendNotificationTool()
        # 用户不存在
        res_err = tool.call(
            {"user_id": "USR_9999", "title": "通知", "content": "测试"},
            self.write_ctx,
        )
        self.assertFalse(res_err.ok)

        # 成功下发短信通知
        res_ok = tool.call(
            {
                "user_id": "USR_001",
                "title": "售后退款受理",
                "content": "您的退款已受理成功",
                "channel": "SMS",
            },
            self.write_ctx,
        )
        self.assertTrue(res_ok.ok)
        self.assertEqual(res_ok.data["channel"], "SMS")
        self.assertEqual(res_ok.data["status"], "SENT")

    def test_escalate_to_human(self):
        tool = EscalateToHumanTool()
        args = {
            "order_id": "ORD_LUXURY_005",
            "reason": "大额商品退款争议",
            "context_summary": "用户要求退款8800元金条，已超自动审核权限，需高阶人工审核",
            "priority": "HIGH",
        }
        res = tool.call(args, self.write_ctx)
        self.assertTrue(res.ok)
        self.assertTrue(res.data["ticket_id"].startswith("TKT_HUMAN_"))
        self.assertEqual(res.data["status"], "ESCALATED_HUMAN")
        self.assertEqual(res.data["priority"], "HIGH")

    async def test_registry_integration_all_tools(self):
        registry = ToolRegistry()
        tools = [
            QueryOrderTool(),
            QueryLogisticsTool(),
            SearchPolicyTool(),
            QueryUserHistoryTool(),
            CreateRefundTool(),
            CreateExchangeOrderTool(),
            InterceptShipmentTool(),
            SendNotificationTool(),
            EscalateToHumanTool(),
        ]
        for t in tools:
            registry.register(t)

        defs = registry.list_definitions()
        self.assertEqual(len(defs), 9)

        # 通过 registry 执行只读工具
        data = await registry.execute("query_order", {"order_id": "ORD_CLOTH_001"})
        self.assertEqual(data["order_id"], "ORD_CLOTH_001")


if __name__ == "__main__":
    unittest.main()
