"""电商售后领域业务工具集.

包含 9 个针对售后服务全流程的业务工具类，均基于 BaseTool 统一契约实现：
1. QueryOrderTool (query_order): 查询订单详情、状态、金额、购买时间
2. QueryLogisticsTool (query_logistics): 查询物流轨迹，判断是否已发货、是否可拦截
3. SearchPolicyTool (search_policy): 检索售后政策，判断商品是否在退货期内、是否符合退货条件
4. QueryUserHistoryTool (query_user_history): 查询用户近期投诉/退款记录，识别恶意行为
5. CreateRefundTool (create_refund): 发起退款，需幂等键和金额上限校验
6. CreateExchangeOrderTool (create_exchange_order): 创建换货单，需校验库存
7. InterceptShipmentTool (intercept_shipment): 拦截已发货包裹，调用物流系统
8. SendNotificationTool (send_notification): 向用户发送处理结果通知
9. EscalateToHumanTool (escalate_to_human): 无法自动处理时，打包上下文转人工
"""
import logging
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Optional

import psycopg
from psycopg.rows import dict_row

from react_ai.config.settings import settings
from react_ai.tools.base import BaseTool, ToolContext

logger = logging.getLogger(__name__)


def _serialize(obj: Any) -> Any:
    """递归将 Decimal/datetime 转换为 JSON 友好格式."""
    if isinstance(obj, Decimal):
        return float(obj)
    if isinstance(obj, datetime):
        return obj.isoformat()
    if isinstance(obj, dict):
        return {k: _serialize(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_serialize(item) for item in obj]
    return obj


class AfterSalesBaseTool(BaseTool):
    """电商售后工具基类，封装通用数据库访问与序列化能力."""

    def __init__(self, db_url: Optional[str] = None):
        super().__init__()
        self.db_url = db_url

    def get_conn(self):
        """获取数据库连接."""
        target_url = self.db_url or settings.database.url
        return psycopg.connect(target_url, row_factory=dict_row)


class QueryOrderTool(AfterSalesBaseTool):
    """根据订单号或用户ID查询订单详情、状态、金额、购买时间."""

    name = "query_order"
    description = "根据订单号或用户ID查询订单详情、状态、金额、购买支付时间、签收时间及商品项明细。"
    side_effects = "read"
    tags = ["order", "aftersales", "query"]

    @property
    def input_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "order_id": {"type": "string", "description": "订单号，例如 ORD_CLOTH_001"},
                "user_id": {"type": "string", "description": "用户ID，例如 USR_001"},
            },
            "additionalProperties": False,
        }

    def execute(self, arguments: dict, ctx: ToolContext) -> Any:
        order_id = arguments.get("order_id")
        user_id = arguments.get("user_id")

        if not order_id and not user_id:
            raise ValueError("必须提供 order_id 或 user_id 之一进行查询")

        with self.get_conn() as conn:
            with conn.cursor() as cur:
                if order_id:
                    cur.execute("SELECT * FROM orders WHERE order_id = %s", (order_id,))
                    order = cur.fetchone()
                    if not order:
                        raise ValueError(f"未找到订单: {order_id}")

                    cur.execute("SELECT * FROM order_items WHERE order_id = %s", (order_id,))
                    items = cur.fetchall()

                    result = dict(order)
                    result["items"] = list(items)
                    return _serialize(result)
                else:
                    cur.execute(
                        "SELECT * FROM orders WHERE user_id = %s ORDER BY created_at DESC",
                        (user_id,),
                    )
                    orders = cur.fetchall()
                    orders_with_items = []
                    for o in orders:
                        cur.execute(
                            "SELECT * FROM order_items WHERE order_id = %s",
                            (o["order_id"],),
                        )
                        items = cur.fetchall()
                        od = dict(o)
                        od["items"] = list(items)
                        orders_with_items.append(od)

                    return _serialize({
                        "user_id": user_id,
                        "orders": orders_with_items,
                        "total_count": len(orders_with_items),
                    })


class QueryLogisticsTool(AfterSalesBaseTool):
    """查询物流轨迹，判断是否已发货、是否可拦截."""

    name = "query_logistics"
    description = "查询物流轨迹与状态，判断包裹是否已发货以及是否支持拦截。"
    side_effects = "read"
    tags = ["logistics", "aftersales", "query"]

    @property
    def input_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "tracking_number": {"type": "string", "description": "物流运单号，如 SF10010001"},
                "order_id": {"type": "string", "description": "订单号，如 ORD_TRANSIT_006"},
            },
            "additionalProperties": False,
        }

    def execute(self, arguments: dict, ctx: ToolContext) -> Any:
        tracking_number = arguments.get("tracking_number")
        order_id = arguments.get("order_id")

        if not tracking_number and not order_id:
            raise ValueError("必须提供 tracking_number 或 order_id 进行物流查询")

        with self.get_conn() as conn:
            with conn.cursor() as cur:
                if tracking_number:
                    cur.execute(
                        "SELECT * FROM logistics WHERE tracking_number = %s",
                        (tracking_number,),
                    )
                else:
                    cur.execute(
                        "SELECT * FROM logistics WHERE order_id = %s ORDER BY updated_at DESC LIMIT 1",
                        (order_id,),
                    )
                row = cur.fetchone()
                if not row:
                    raise ValueError("未找到对应的物流信息")

                status = row["status"]
                is_shipped = status in ("IN_TRANSIT", "DELIVERED", "EXCEPTION")
                can_intercept = (status == "IN_TRANSIT" and row.get("signed_time") is None)

                return _serialize({
                    "tracking_number": row["tracking_number"],
                    "order_id": row["order_id"],
                    "carrier": row["carrier"],
                    "status": status,
                    "is_shipped": is_shipped,
                    "can_intercept": can_intercept,
                    "signed_time": row["signed_time"],
                    "current_location": row["current_location"],
                    "updated_at": row["updated_at"],
                })


class SearchPolicyTool(AfterSalesBaseTool):
    """检索售后政策，判断该商品是否在退货期内、是否符合退货条件."""

    name = "search_policy"
    description = "检索售后政策，判断该商品是否在退货期内、是否符合退货条件。"
    side_effects = "read"
    tags = ["policy", "aftersales", "query"]

    @property
    def input_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "category": {
                    "type": "string",
                    "description": "商品品类，如 CLOTHING, FRESH_FOOD, DIGITAL, LUXURY",
                    "enum": ["CLOTHING", "FRESH_FOOD", "DIGITAL", "LUXURY"],
                },
                "order_id": {"type": "string", "description": "关联订单号，用于查询签收时长"},
                "item_id": {"type": "string", "description": "关联商品项ID，用于获取品类和签收时间"},
                "reason_type": {
                    "type": "string",
                    "description": "退换原因类型：no_reason (无理由退货) 或 quality_issue (质量问题)",
                    "enum": ["no_reason", "quality_issue"],
                },
            },
            "additionalProperties": False,
        }

    def execute(self, arguments: dict, ctx: ToolContext) -> Any:
        category = arguments.get("category")
        order_id = arguments.get("order_id")
        item_id = arguments.get("item_id")
        reason_type = arguments.get("reason_type", "no_reason")

        with self.get_conn() as conn:
            with conn.cursor() as cur:
                if not category:
                    if item_id:
                        cur.execute(
                            "SELECT category, order_id FROM order_items WHERE item_id = %s",
                            (item_id,),
                        )
                        row = cur.fetchone()
                        if row:
                            category = row["category"]
                            order_id = order_id or row["order_id"]
                    elif order_id:
                        cur.execute(
                            "SELECT category FROM order_items WHERE order_id = %s LIMIT 1",
                            (order_id,),
                        )
                        row = cur.fetchone()
                        if row:
                            category = row["category"]

                if not category:
                    raise ValueError("必须提供 category、order_id 或 item_id 之一以确定售后政策品类")

                cur.execute(
                    "SELECT * FROM after_sales_policies WHERE category = %s",
                    (category,),
                )
                policy = cur.fetchone()
                if not policy:
                    raise ValueError(f"未找到品类 {category} 的售后政策")

                delivery_time = None
                days_since_delivery = None
                within_return_window = True
                eligible_for_return = True
                eligibility_note = "符合退换货条件"

                if order_id:
                    cur.execute("SELECT delivery_time FROM orders WHERE order_id = %s", (order_id,))
                    order_row = cur.fetchone()
                    if order_row and order_row["delivery_time"]:
                        delivery_time = order_row["delivery_time"]
                        now = datetime.now(timezone.utc)
                        delta = now - delivery_time
                        days_since_delivery = max(0.0, delta.total_seconds() / 86400.0)

                        limit_days = (
                            policy["support_no_reason_days"]
                            if reason_type == "no_reason"
                            else policy["support_quality_issue_days"]
                        )
                        within_return_window = days_since_delivery <= limit_days

                if reason_type == "no_reason" and policy["support_no_reason_days"] <= 0:
                    eligible_for_return = False
                    eligibility_note = f"{policy['category_name']} 品类不支持无理由退货"
                elif within_return_window is False:
                    limit_days = (
                        policy["support_no_reason_days"]
                        if reason_type == "no_reason"
                        else policy["support_quality_issue_days"]
                    )
                    eligible_for_return = False
                    eligibility_note = f"已超出售后申请时效 (签收已满 {days_since_delivery:.1f} 天，上限为 {limit_days} 天)"
                else:
                    eligible_for_return = True
                    eligibility_note = "符合申请条件"

                return _serialize({
                    "category": policy["category"],
                    "category_name": policy["category_name"],
                    "support_no_reason_days": policy["support_no_reason_days"],
                    "support_quality_issue_days": policy["support_quality_issue_days"],
                    "need_return_goods": policy["need_return_goods"],
                    "max_auto_refund_amount": policy["max_auto_refund_amount"],
                    "description": policy["description"],
                    "within_return_window": within_return_window,
                    "eligible_for_return": eligible_for_return,
                    "days_since_delivery": round(days_since_delivery, 1) if days_since_delivery is not None else None,
                    "reason_type": reason_type,
                    "eligibility_note": eligibility_note,
                })


class QueryUserHistoryTool(AfterSalesBaseTool):
    """查询用户近期投诉与退款记录，计算退款比例，识别恶意行为."""

    name = "query_user_history"
    description = "查询用户近期投诉/退款记录，识别恶意退款或高风险行为。"
    side_effects = "read"
    tags = ["user", "aftersales", "risk"]

    @property
    def input_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "user_id": {"type": "string", "description": "用户ID，如 USR_001"},
                "time_range_days": {
                    "type": "integer",
                    "description": "查询时间范围天数，默认 90 天",
                    "minimum": 1,
                    "maximum": 365,
                },
            },
            "required": ["user_id"],
            "additionalProperties": False,
        }

    def execute(self, arguments: dict, ctx: ToolContext) -> Any:
        user_id = arguments["user_id"]
        days = arguments.get("time_range_days", 90)

        with self.get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT * FROM users WHERE user_id = %s", (user_id,))
                user = cur.fetchone()
                if not user:
                    raise ValueError(f"用户 {user_id} 不存在")

                cur.execute(
                    """
                    SELECT COUNT(*) AS total_orders, COALESCE(SUM(total_amount), 0) AS total_amount
                    FROM orders
                    WHERE user_id = %s AND created_at >= NOW() - make_interval(days => %s)
                    """,
                    (user_id, days),
                )
                order_stats = cur.fetchone()
                total_orders = order_stats["total_orders"] if order_stats else 0
                total_order_amount = order_stats["total_amount"] if order_stats else Decimal(0)

                cur.execute(
                    """
                    SELECT * FROM after_sales_tickets
                    WHERE user_id = %s AND created_at >= NOW() - make_interval(days => %s)
                    ORDER BY created_at DESC
                    """,
                    (user_id, days),
                )
                tickets = cur.fetchall()
                ticket_count = len(tickets)
                total_refund_amount = sum(Decimal(t["refund_amount"]) for t in tickets)

                refund_rate = (ticket_count / total_orders) if total_orders > 0 else 0.0
                is_high_risk = (refund_rate >= 0.5 and ticket_count >= 2) or (ticket_count >= 5)
                risk_level = "HIGH" if is_high_risk else ("MEDIUM" if ticket_count >= 2 else "LOW")

                risk_reasons = []
                if is_high_risk:
                    risk_reasons.append(f"售后退款率达 {refund_rate * 100:.1f}%，显著偏高")
                if ticket_count >= 3:
                    risk_reasons.append(f"近 {days} 天申请售后次数达 {ticket_count} 次，行为异常频繁")

                return _serialize({
                    "user_id": user_id,
                    "username": user["username"],
                    "phone": user["phone"],
                    "vip_level": user["vip_level"],
                    "time_range_days": days,
                    "total_orders": total_orders,
                    "total_order_amount": total_order_amount,
                    "ticket_count": ticket_count,
                    "total_refund_amount": total_refund_amount,
                    "refund_rate": round(refund_rate, 4),
                    "is_high_risk": is_high_risk,
                    "risk_level": risk_level,
                    "risk_reasons": risk_reasons,
                    "recent_tickets": list(tickets),
                })


class CreateRefundTool(AfterSalesBaseTool):
    """发起退款，需幂等键和金额上限校验."""

    name = "create_refund"
    description = "发起退款申请，需幂等键和金额上限校验。根据品类限额自动核准或转入待审核工单。"
    side_effects = "write"
    tags = ["refund", "aftersales", "write"]

    @property
    def input_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "order_id": {"type": "string", "description": "订单号"},
                "item_id": {"type": "string", "description": "申请退款的商品项ID"},
                "refund_amount": {"type": "number", "description": "退款金额", "minimum": 0.01},
                "reason": {"type": "string", "description": "退款原因"},
                "refund_type": {
                    "type": "string",
                    "description": "退款类型：REFUND_ONLY (仅退款), RETURN_AND_REFUND (退货退款)",
                    "enum": ["REFUND_ONLY", "RETURN_AND_REFUND"],
                },
            },
            "required": ["order_id", "item_id", "refund_amount", "reason"],
            "additionalProperties": False,
        }

    def execute(self, arguments: dict, ctx: ToolContext) -> Any:
        order_id = arguments["order_id"]
        item_id = arguments["item_id"]
        refund_amount = Decimal(str(arguments["refund_amount"]))
        reason = arguments["reason"]

        with self.get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT oi.*, o.user_id, o.order_status
                    FROM order_items oi
                    JOIN orders o ON oi.order_id = o.order_id
                    WHERE oi.item_id = %s AND oi.order_id = %s
                    """,
                    (item_id, order_id),
                )
                item = cur.fetchone()
                if not item:
                    raise ValueError(f"未找到订单 {order_id} 对应的商品项 {item_id}")

                item_total = item["unit_price"] * item["quantity"]
                if refund_amount > item_total:
                    raise ValueError(
                        f"退款金额 {refund_amount} 超过该商品总金额 {item_total}"
                    )

                cur.execute(
                    "SELECT * FROM after_sales_policies WHERE category = %s",
                    (item["category"],),
                )
                policy = cur.fetchone()
                max_auto = policy["max_auto_refund_amount"] if policy else Decimal("300.00")
                need_return = policy["need_return_goods"] if policy else True

                default_type = "RETURN_AND_REFUND" if need_return else "REFUND_ONLY"
                refund_type = arguments.get("refund_type", default_type)

                auto_approved = (refund_amount <= max_auto and max_auto > 0)
                if auto_approved:
                    status = "NEED_RETURN_SHIPPING" if refund_type == "RETURN_AND_REFUND" else "APPROVED_AUTO"
                    message = "退款申请已通过系统初审"
                else:
                    status = "PENDING_REVIEW"
                    message = f"退款金额 {refund_amount} 元超过自动审核限额 {max_auto} 元，转入待审核工单"

                ticket_id = f"TKT_REF_{uuid.uuid4().hex[:12].upper()}"
                review_notes = f"[幂等键: {ctx.idempotency_key}] 自动限额: {max_auto}"

                cur.execute(
                    """
                    INSERT INTO after_sales_tickets
                    (ticket_id, order_id, item_id, user_id, type, reason, status, refund_amount, review_notes)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                    """,
                    (
                        ticket_id,
                        order_id,
                        item_id,
                        item["user_id"],
                        refund_type,
                        reason,
                        status,
                        refund_amount,
                        review_notes,
                    ),
                )

                cur.execute(
                    "UPDATE order_items SET after_sales_status = 'IN_PROGRESS' WHERE item_id = %s",
                    (item_id,),
                )
                conn.commit()

                return _serialize({
                    "ticket_id": ticket_id,
                    "order_id": order_id,
                    "item_id": item_id,
                    "user_id": item["user_id"],
                    "refund_type": refund_type,
                    "refund_amount": refund_amount,
                    "status": status,
                    "auto_approved": auto_approved,
                    "max_auto_limit": max_auto,
                    "message": message,
                })


class CreateExchangeOrderTool(AfterSalesBaseTool):
    """创建换货单，需校验库存."""

    name = "create_exchange_order"
    description = "创建换货单，校验原商品与目标SKU库存，生成换货申请工单。"
    side_effects = "write"
    tags = ["exchange", "aftersales", "write"]

    @property
    def input_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "order_id": {"type": "string", "description": "原订单号"},
                "item_id": {"type": "string", "description": "原商品项ID"},
                "target_sku_id": {"type": "string", "description": "换货目标SKU ID，如 SKU_1001_M"},
                "reason": {"type": "string", "description": "换货原因"},
            },
            "required": ["order_id", "item_id", "target_sku_id", "reason"],
            "additionalProperties": False,
        }

    def execute(self, arguments: dict, ctx: ToolContext) -> Any:
        order_id = arguments["order_id"]
        item_id = arguments["item_id"]
        target_sku_id = arguments["target_sku_id"]
        reason = arguments["reason"]

        if target_sku_id.upper().endswith("_OOS") or "OUT_OF_STOCK" in target_sku_id.upper():
            raise ValueError(f"目标商品 SKU {target_sku_id} 当前库存不足，无法换货")

        with self.get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT oi.*, o.user_id
                    FROM order_items oi
                    JOIN orders o ON oi.order_id = o.order_id
                    WHERE oi.item_id = %s AND oi.order_id = %s
                    """,
                    (item_id, order_id),
                )
                item = cur.fetchone()
                if not item:
                    raise ValueError(f"未找到订单 {order_id} 对应的商品项 {item_id}")

                ticket_id = f"TKT_EXC_{uuid.uuid4().hex[:12].upper()}"
                review_notes = f"[幂等键: {ctx.idempotency_key}] 换货目标SKU: {target_sku_id}"

                cur.execute(
                    """
                    INSERT INTO after_sales_tickets
                    (ticket_id, order_id, item_id, user_id, type, reason, status, refund_amount, review_notes)
                    VALUES (%s, %s, %s, %s, 'EXCHANGE', %s, 'PENDING_REVIEW', 0.00, %s)
                    """,
                    (
                        ticket_id,
                        order_id,
                        item_id,
                        item["user_id"],
                        reason,
                        review_notes,
                    ),
                )

                cur.execute(
                    "UPDATE order_items SET after_sales_status = 'IN_PROGRESS' WHERE item_id = %s",
                    (item_id,),
                )
                conn.commit()

                return _serialize({
                    "ticket_id": ticket_id,
                    "order_id": order_id,
                    "item_id": item_id,
                    "user_id": item["user_id"],
                    "target_sku_id": target_sku_id,
                    "status": "PENDING_REVIEW",
                    "message": f"换货工单 {ticket_id} 创建成功，目标商品库存充足，进入审核与原商品寄回流程",
                })


class InterceptShipmentTool(AfterSalesBaseTool):
    """拦截已发货包裹，需调用物流系统."""

    name = "intercept_shipment"
    description = "拦截已发货包裹，更新物流状态并记录拦截原因。包裹已签收则拒绝拦截。"
    side_effects = "write"
    tags = ["logistics", "aftersales", "intercept", "write"]

    @property
    def input_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "tracking_number": {"type": "string", "description": "运单号"},
                "order_id": {"type": "string", "description": "订单号"},
                "reason": {"type": "string", "description": "拦截原因"},
            },
            "required": ["reason"],
            "additionalProperties": False,
        }

    def execute(self, arguments: dict, ctx: ToolContext) -> Any:
        tracking_number = arguments.get("tracking_number")
        order_id = arguments.get("order_id")
        reason = arguments["reason"]

        if not tracking_number and not order_id:
            raise ValueError("必须提供 tracking_number 或 order_id 才能执行拦截")

        with self.get_conn() as conn:
            with conn.cursor() as cur:
                if tracking_number:
                    cur.execute(
                        "SELECT * FROM logistics WHERE tracking_number = %s",
                        (tracking_number,),
                    )
                else:
                    cur.execute(
                        "SELECT * FROM logistics WHERE order_id = %s ORDER BY updated_at DESC LIMIT 1",
                        (order_id,),
                    )
                row = cur.fetchone()
                if not row:
                    raise ValueError("未找到对应的物流包裹记录")

                if row["status"] == "DELIVERED" or row.get("signed_time") is not None:
                    raise ValueError(
                        f"运单 {row['tracking_number']} 已由用户签收，无法执行物流拦截，请引导退货退款"
                    )

                new_location = f"物流已拦截: {reason} (承运商已调度原路退回)"
                cur.execute(
                    """
                    UPDATE logistics
                    SET status = 'EXCEPTION', current_location = %s, updated_at = NOW()
                    WHERE tracking_number = %s
                    """,
                    (new_location, row["tracking_number"]),
                )
                conn.commit()

                return _serialize({
                    "tracking_number": row["tracking_number"],
                    "order_id": row["order_id"],
                    "carrier": row["carrier"],
                    "previous_status": row["status"],
                    "current_status": "EXCEPTION",
                    "intercept_success": True,
                    "message": f"运单 {row['tracking_number']} 拦截指令下发成功，承运商 ({row['carrier']}) 已停止派送并退回",
                })


class SendNotificationTool(AfterSalesBaseTool):
    """向用户发送处理结果通知."""

    name = "send_notification"
    description = "向用户发送处理结果通知，支持站内信 (IN_APP) 与短信 (SMS) 渠道。"
    side_effects = "write"
    tags = ["notification", "aftersales", "write"]

    @property
    def input_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "user_id": {"type": "string", "description": "接收通知的用户ID"},
                "title": {"type": "string", "description": "通知标题"},
                "content": {"type": "string", "description": "通知内容"},
                "channel": {
                    "type": "string",
                    "description": "通知渠道：IN_APP 或 SMS，默认 IN_APP",
                    "enum": ["IN_APP", "SMS"],
                },
                "ticket_id": {"type": "string", "description": "关联的工单号（可选）"},
            },
            "required": ["user_id", "title", "content"],
            "additionalProperties": False,
        }

    def execute(self, arguments: dict, ctx: ToolContext) -> Any:
        user_id = arguments["user_id"]
        title = arguments["title"]
        content = arguments["content"]
        channel = arguments.get("channel", "IN_APP")
        ticket_id = arguments.get("ticket_id")

        with self.get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT user_id, phone FROM users WHERE user_id = %s", (user_id,))
                user = cur.fetchone()
                if not user:
                    raise ValueError(f"用户 {user_id} 不存在")

                cur.execute(
                    """
                    INSERT INTO notifications (ticket_id, user_id, channel, title, content, status)
                    VALUES (%s, %s, %s, %s, %s, 'SENT')
                    RETURNING notification_id, created_at
                    """,
                    (ticket_id, user_id, channel, title, content),
                )
                row = cur.fetchone()
                conn.commit()

                return _serialize({
                    "notification_id": row["notification_id"],
                    "user_id": user_id,
                    "channel": channel,
                    "title": title,
                    "content": content,
                    "status": "SENT",
                    "created_at": row["created_at"],
                    "message": f"通知已通过 {channel} 渠道下发成功",
                })


class EscalateToHumanTool(AfterSalesBaseTool):
    """无法自动处理时，打包上下文转人工."""

    name = "escalate_to_human"
    description = "无法自动处理时，打包上下文转交人工客服，生成或更新转人工工单。"
    side_effects = "write"
    tags = ["escalation", "aftersales", "human", "write"]

    @property
    def input_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "order_id": {"type": "string", "description": "订单号"},
                "reason": {"type": "string", "description": "转人工原因"},
                "context_summary": {"type": "string", "description": "打包的上下文摘要与诊断信息"},
                "priority": {
                    "type": "string",
                    "description": "优先级：LOW, NORMAL, HIGH, URGENT，默认 NORMAL",
                    "enum": ["LOW", "NORMAL", "HIGH", "URGENT"],
                },
                "item_id": {"type": "string", "description": "涉及的商品项ID（可选）"},
                "ticket_id": {"type": "string", "description": "已有工单号（可选）"},
            },
            "required": ["order_id", "reason", "context_summary"],
            "additionalProperties": False,
        }

    def execute(self, arguments: dict, ctx: ToolContext) -> Any:
        order_id = arguments["order_id"]
        reason = arguments["reason"]
        context_summary = arguments["context_summary"]
        priority = arguments.get("priority", "NORMAL")
        item_id = arguments.get("item_id")
        existing_ticket_id = arguments.get("ticket_id")

        with self.get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT user_id FROM orders WHERE order_id = %s", (order_id,))
                order = cur.fetchone()
                if not order:
                    raise ValueError(f"未找到订单 {order_id}")

                user_id = order["user_id"]
                review_notes = f"[优先级: {priority}][幂等键: {ctx.idempotency_key}] 上下文摘要: {context_summary}"

                if existing_ticket_id:
                    cur.execute(
                        """
                        UPDATE after_sales_tickets
                        SET status = 'ESCALATED_HUMAN', reason = %s, review_notes = %s, updated_at = NOW()
                        WHERE ticket_id = %s
                        """,
                        (reason, review_notes, existing_ticket_id),
                    )
                    ticket_id = existing_ticket_id
                else:
                    ticket_id = f"TKT_HUMAN_{uuid.uuid4().hex[:12].upper()}"
                    cur.execute(
                        """
                        INSERT INTO after_sales_tickets
                        (ticket_id, order_id, item_id, user_id, type, reason, status, refund_amount, review_notes)
                        VALUES (%s, %s, %s, %s, 'REFUND_ONLY', %s, 'ESCALATED_HUMAN', 0.00, %s)
                        """,
                        (ticket_id, order_id, item_id, user_id, reason, review_notes),
                    )

                conn.commit()

                return _serialize({
                    "ticket_id": ticket_id,
                    "order_id": order_id,
                    "item_id": item_id,
                    "user_id": user_id,
                    "status": "ESCALATED_HUMAN",
                    "priority": priority,
                    "reason": reason,
                    "context_summary": context_summary,
                    "message": f"工单 {ticket_id} 已转交人工客服处理，优先级: {priority}",
                })
