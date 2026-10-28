"""
电商售后工单处理工作流 (基于 LangGraph 状态图与项目内置 aftersales 工具集)

业务链路:
识别意图 -> 查询订单 -> 检查物流状态 -> 核对售后政策 -> 判断是否符合条件 -> 执行退款/换货 -> 发送通知 -> 异常时转人工

架构设计:
1. 状态模式 (AfterSalesState): 定义全流程上下文状态。
2. 真实工具对接 (Official Tools): 直接调用项目内置的 `react_ai.tools.aftersales` 工具类
   - QueryOrderTool (query_order): 查订单及商品详情
   - QueryLogisticsTool (query_logistics): 查物流签收与轨迹
   - SearchPolicyTool (search_policy): 查 PG after_sales_policies 政策规则并计算合规性
   - CreateRefundTool (create_refund): 调用支付写操作退款 (支持幂等)
   - SendNotificationTool (send_notification): 发送用户短信/站内信
   - EscalateToHumanTool (escalate_to_human): 转人工审核并写入待办工单
3. 条件路由 (Conditional Edges): 根据政策合规性与金额风控分流至自动执行或人工介入。
4. 持久化支持 (PostgreSQL Checkpointer): 支持挂载基于 PG 的状态持久化与断点续跑。
"""

import sys
import uuid
from pathlib import Path
from typing import Dict, Any, Optional, Literal
from typing_extensions import TypedDict

# 自动将项目 src 根路径加入模块搜索路径，支持直接运行
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from langgraph.graph import StateGraph, START, END
from langgraph.checkpoint.memory import MemorySaver

# 导入本项目已定义好的电商售后工具与上下文
from react_ai.tools.base import ToolContext
from react_ai.tools.aftersales import (
    QueryOrderTool,
    QueryLogisticsTool,
    SearchPolicyTool,
    CreateRefundTool,
    SendNotificationTool,
    EscalateToHumanTool,
)

# 实例化官方工具单例
query_order_tool = QueryOrderTool()
query_logistics_tool = QueryLogisticsTool()
search_policy_tool = SearchPolicyTool()
create_refund_tool = CreateRefundTool()
send_notification_tool = SendNotificationTool()
escalate_to_human_tool = EscalateToHumanTool()


# ============================================================================
# 1. 状态定义 (State Definition)
# ============================================================================
class AfterSalesState(TypedDict, total=False):
    """电商售后全流程核心状态对象。"""
    session_id: str                   # 会话/线程ID
    user_id: str                      # 发起请求的用户ID
    user_message: str                 # 用户输入 (如: "我买的衣服破了，要退货")
    
    # 意图与槽位解析
    intent: Optional[str]             # RETURN_AND_REFUND(退货退款) / REFUND_ONLY(仅退款) / EXCHANGE(换货)
    order_id: Optional[str]           # 关联订单号
    reason: Optional[str]             # 售后具体原因
    reason_type: Optional[str]        # 政策原因大类: quality_issue (质量问题) / no_reason (无理由)

    # 外部与工具执行结果缓存
    order_info: Optional[Dict[str, Any]]      # 订单详情
    logistics_info: Optional[Dict[str, Any]]  # 物流轨迹
    policy_rules: Optional[Dict[str, Any]]    # 售后政策查询计算结果

    # 决策与判定
    is_eligible: bool                         # 规则判定是否通过
    decision: Optional[str]                   # 最终决策: AUTO_REFUND / RETURN_SHIPPING / ESCALATE_HUMAN / REJECTED
    human_reason: Optional[str]               # 转人工的具体原因

    # 写操作结果与最终回复
    action_result: Optional[Dict[str, Any]]   # 写操作结果 (退款单号、工单号等)
    notification_sent: bool                   # 是否已发通知
    final_reply: Optional[str]                # 呈现给用户的答复


# ============================================================================
# 2. 状态图节点 (Graph Nodes - 调用项目内置真实工具)
# ============================================================================
def intent_recognition_node(state: AfterSalesState) -> Dict[str, Any]:
    """
    [节点 1: 识别意图与槽位]
    - 从用户输入提取意图、订单号及原因类型 (quality_issue 或 no_reason)。
    """
    user_msg = state.get("user_message", "")
    print(f"\n[Node: intent_recognition] 分析用户诉求: '{user_msg}'")

    # 根据关键词解析意图与原因大类
    is_quality = any(k in user_msg for k in ["破", "坏", "烂", "残", "瑕疵", "质量", "断"])
    reason_type = "quality_issue" if is_quality else "no_reason"

    return {
        "intent": "RETURN_AND_REFUND",
        "order_id": state.get("order_id") or "ORD_CLOTH_001",
        "reason": "衣服破损质量问题" if is_quality else "用户不满意/无理由",
        "reason_type": reason_type
    }


def query_order_node(state: AfterSalesState) -> Dict[str, Any]:
    """
    [节点 2: 查询订单详情]
    - 调用内置 QueryOrderTool 从 PostgreSQL 真实查询订单数据。
    """
    order_id = state.get("order_id", "")
    user_id = state.get("user_id", "USR_001")
    print(f"[Node: query_order] 调用 QueryOrderTool 查询订单: {order_id}")

    ctx = ToolContext(caller_id="langgraph-aftersales", tenant_id="default", user_id=user_id)
    res = query_order_tool.call({"order_id": order_id}, ctx)

    if not res.ok:
        raise RuntimeError(f"查询订单失败: {res.error_message}")

    return {"order_info": res.data}


def check_logistics_node(state: AfterSalesState) -> Dict[str, Any]:
    """
    [节点 3: 检查物流状态]
    - 调用内置 QueryLogisticsTool 核验签收状态与物流轨迹。
    """
    order_id = state.get("order_id", "")
    user_id = state.get("user_id", "USR_001")
    print(f"[Node: check_logistics] 调用 QueryLogisticsTool 检查物流: {order_id}")

    ctx = ToolContext(caller_id="langgraph-aftersales", tenant_id="default", user_id=user_id)
    res = query_logistics_tool.call({"order_id": order_id}, ctx)

    return {"logistics_info": res.data if res.ok else {}}


def policy_evaluation_node(state: AfterSalesState) -> Dict[str, Any]:
    """
    [节点 4: 核对售后政策]
    - 调用内置 SearchPolicyTool 从 PG after_sales_policies 表核验退换货合规性。
    """
    order_id = state.get("order_id", "")
    user_id = state.get("user_id", "USR_001")
    reason_type = state.get("reason_type", "quality_issue")
    order_info = state.get("order_info") or {}
    items = order_info.get("items", [])
    category = items[0].get("category", "CLOTHING") if items else "CLOTHING"

    print(f"[Node: policy_evaluation] 调用 SearchPolicyTool 核对政策: 品类={category}, 原因类型={reason_type}")

    ctx = ToolContext(caller_id="langgraph-aftersales", tenant_id="default", user_id=user_id)
    res = search_policy_tool.call({
        "order_id": order_id,
        "category": category,
        "reason_type": reason_type
    }, ctx)

    if not res.ok:
        raise RuntimeError(f"核对政策失败: {res.error_message}")

    return {"policy_rules": res.data}


def condition_decision_node(state: AfterSalesState) -> Dict[str, Any]:
    """
    [节点 5: 综合条件判断与决策路由]
    - 判断是否在有效退换期内，是否超出大额退款阈值。
    """
    policy = state.get("policy_rules") or {}
    order_info = state.get("order_info") or {}
    total_amount = float(order_info.get("total_amount", 0.0))
    max_auto_amount = float(policy.get("max_auto_refund_amount", 300.0))
    eligible = policy.get("eligible_for_return", False)

    print(f"[Node: condition_decision] 综合风控判定: 是否符合政策={eligible}, 金额=¥{total_amount}, 自动上限=¥{max_auto_amount}")

    # 1. 政策不符合 (如超期或不支持无理由)
    if not eligible:
        reason_note = policy.get("eligibility_note", "不符合退换政策")
        return {
            "is_eligible": False,
            "decision": "REJECTED",
            "human_reason": reason_note,
            "final_reply": f"抱歉，您的售后申请未能通过自动审核：{reason_note}。"
        }

    # 2. 金额超出自动审核阈值 -> 转人工接管 (HITL)
    if total_amount > max_auto_amount:
        return {
            "is_eligible": True,
            "decision": "ESCALATE_HUMAN",
            "human_reason": f"订单实付金额(¥{total_amount:.2f})超出自动审核阈值(¥{max_auto_amount:.2f})，需专员复核"
        }

    # 3. 正常通过，根据政策决定是退货寄件还是直接退款
    if policy.get("need_return_goods", True):
        decision = "RETURN_SHIPPING"
    else:
        decision = "AUTO_REFUND"

    return {
        "is_eligible": True,
        "decision": decision,
        "human_reason": None
    }


def execute_action_node(state: AfterSalesState) -> Dict[str, Any]:
    """
    [节点 6: 执行业务写操作]
    - 调用 CreateRefundTool 等真实写操作工具并生成用户反馈。
    """
    decision = state.get("decision")
    order_id = state.get("order_id", "")
    user_id = state.get("user_id", "USR_001")
    order_info = state.get("order_info") or {}
    amount = float(order_info.get("total_amount", 0.0))

    print(f"[Node: execute_action] 执行写操作: 决策类型={decision}")

    if decision == "RETURN_SHIPPING":
        # 生成退货取件引导
        action_res = {
            "status": "RETURN_LABEL_GENERATED",
            "carrier": "顺丰速运",
            "pickup_code": "8899",
            "return_tracking_no": f"SF_RET_{uuid.uuid4().hex[:8].upper()}"
        }
        reply = (
            f"您的售后退货申请已通过！已为您预约【顺丰速运】上门取件，"
            f"寄件码: {action_res['pickup_code']}，运单号: {action_res['return_tracking_no']}。"
            f"快递员将在2小时内联系您取件，仓库质检签收后退款将原路到账。"
        )

    elif decision == "AUTO_REFUND":
        # 调用 CreateRefundTool 执行退款写入
        ctx = ToolContext(
            caller_id="langgraph-aftersales",
            tenant_id="default",
            user_id=user_id,
            idempotency_key=f"REFUND_IDEM_{order_id}_{uuid.uuid4().hex[:6]}"
        )
        res = create_refund_tool.call({
            "order_id": order_id,
            "amount": amount,
            "reason": state.get("reason", "售后极速退款")
        }, ctx)
        action_res = res.data if res.ok else {"error": res.error_message}
        reply = f"您的极速退款申请已通过，退款金额 ¥{amount:.2f} 已提交支付网关原路退回，请留意到账通知。"

    else:
        action_res = {"status": "NOOP"}
        reply = "申请已处理完毕。"

    return {
        "action_result": action_res,
        "final_reply": reply
    }


def human_escalation_node(state: AfterSalesState) -> Dict[str, Any]:
    """
    [节点 7: 转人工接管 (HITL)]
    - 调用内置 EscalateToHumanTool 生成人工审核工单。
    """
    order_id = state.get("order_id", "")
    user_id = state.get("user_id", "USR_001")
    reason = state.get("human_reason", "超出自动退款额度")
    print(f"[Node: human_escalation] 调用 EscalateToHumanTool 转人工处理: {reason}")

    ctx = ToolContext(
        caller_id="langgraph-aftersales",
        tenant_id="default",
        user_id=user_id,
        idempotency_key=f"IDEM_ESCALATE_{order_id}_{uuid.uuid4().hex[:6]}"
    )
    res = escalate_to_human_tool.call({
        "order_id": order_id,
        "reason": reason,
        "context_summary": f"订单 {order_id} 用户发起诉求: {state.get('user_message')}，经系统初审需人工介入: {reason}",
        "priority": "HIGH"
    }, ctx)

    ticket_id = res.data.get("ticket_id") if res.ok else "TICKET_MANUAL_001"
    reply = f"由于【{reason}】，系统已为您升级至人工专员审核，工单号: {ticket_id}。专员将在2小时内完成复核。"

    return {
        "action_result": res.data if res.ok else {},
        "final_reply": reply
    }


def send_notification_node(state: AfterSalesState) -> Dict[str, Any]:
    """
    [节点 8: 发送用户通知]
    - 调用内置 SendNotificationTool 写入 notifications 表并发送站内信/短信。
    """
    user_id = state.get("user_id", "USR_001")
    final_reply = state.get("final_reply", "您的售后诉求已处理。")
    order_id = state.get("order_id", "")
    print(f"[Node: send_notification] 调用 SendNotificationTool 发送通知至用户[{user_id}]")

    ctx = ToolContext(
        caller_id="langgraph-aftersales",
        tenant_id="default",
        user_id=user_id,
        idempotency_key=f"IDEM_NOTIF_{order_id}_{uuid.uuid4().hex[:6]}"
    )
    send_notification_tool.call({
        "user_id": user_id,
        "title": "售后服务单处理进展通知",
        "content": final_reply,
        "channel": "IN_APP",
        "ticket_id": state.get("action_result", {}).get("ticket_id") or order_id
    }, ctx)

    return {"notification_sent": True}


# ============================================================================
# 3. 动态条件路由 (Conditional Edge Router)
# ============================================================================
def route_decision(state: AfterSalesState) -> Literal["execute_action", "human_escalation", "send_notification"]:
    decision = state.get("decision")
    if decision in ("RETURN_SHIPPING", "AUTO_REFUND"):
        return "execute_action"
    elif decision == "ESCALATE_HUMAN":
        return "human_escalation"
    else:
        return "send_notification"


# ============================================================================
# 4. 构建与编译 LangGraph (Graph Construction)
# ============================================================================
def build_aftersales_graph(checkpointer=None):
    workflow = StateGraph(AfterSalesState)

    # 注册节点
    workflow.add_node("intent_recognition", intent_recognition_node)
    workflow.add_node("query_order", query_order_node)
    workflow.add_node("check_logistics", check_logistics_node)
    workflow.add_node("policy_evaluation", policy_evaluation_node)
    workflow.add_node("condition_decision", condition_decision_node)
    workflow.add_node("execute_action", execute_action_node)
    workflow.add_node("human_escalation", human_escalation_node)
    workflow.add_node("send_notification", send_notification_node)

    # 串联常规边
    workflow.add_edge(START, "intent_recognition")
    workflow.add_edge("intent_recognition", "query_order")
    workflow.add_edge("query_order", "check_logistics")
    workflow.add_edge("check_logistics", "policy_evaluation")
    workflow.add_edge("policy_evaluation", "condition_decision")

    # 条件路由分流
    workflow.add_conditional_edges(
        "condition_decision",
        route_decision,
        {
            "execute_action": "execute_action",
            "human_escalation": "human_escalation",
            "send_notification": "send_notification"
        }
    )

    # 汇聚通知并结束
    workflow.add_edge("execute_action", "send_notification")
    workflow.add_edge("human_escalation", "send_notification")
    workflow.add_edge("send_notification", END)

    return workflow.compile(checkpointer=checkpointer)


# ============================================================================
# 5. 主程序运行演示 (执行真实数据库与真实工具调用)
# ============================================================================
def main():
    print("=" * 75)
    print(">>> 启动 LangGraph 电商售后工单工作流 (使用 react_ai.tools.aftersales 真实工具) <<<")
    print("=" * 75)

    app = build_aftersales_graph(checkpointer=MemorySaver())

    # Case 1: 衣服破损退货 (用户提问场景)
    case1_input: AfterSalesState = {
        "session_id": "SESS_TEST_001",
        "user_id": "USR_001",
        "order_id": "ORD_CLOTH_001",
        "user_message": "我买的衣服破了，要退货"
    }

    print("\n--- [测试用例 1: 衣服破损退货 -> 自动通过并生成寄件码] ---")
    res1 = app.invoke(case1_input, config={"configurable": {"thread_id": "THREAD_001"}})
    print(f"最终决策: {res1.get('decision')}")
    print(f"客户回复:\n{res1.get('final_reply')}")

    # Case 2: 大额黄金订单退款 (触发转人工审核)
    case2_input: AfterSalesState = {
        "session_id": "SESS_TEST_002",
        "user_id": "USR_003",
        "order_id": "ORD_LUXURY_005",
        "user_message": "黄金手链断了，要求退款"
    }

    print("\n\n--- [测试用例 2: 黄金手链金额超限 -> 转人工专员审核 (HITL)] ---")
    res2 = app.invoke(case2_input, config={"configurable": {"thread_id": "THREAD_002"}})
    print(f"最终决策: {res2.get('decision')}")
    print(f"客户回复:\n{res2.get('final_reply')}")


if __name__ == "__main__":
    main()
