"""流程编排示例 (Flow Demo - BaseFlow 演示同步、异步与重试编排)."""
import asyncio
import sys
from pathlib import Path

# 自动将项目 src 根路径加入模块搜索路径，支持直接运行
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from react_ai.flows.base import (
    BaseFlow,
    FlowContext,
    FlowPriority,
)


class OrderFulfillmentFlow(BaseFlow):
    """订单履约处理流程：包含参数校验、风控检查、库存锁定与支付通知。"""

    name = "order_fulfillment_flow"
    version = "1.0.0"
    priority = FlowPriority.HIGH
    timeout_seconds = 30.0

    def build_steps(self):
        self.register_step("validate_order", self.validate_order, description="校验订单输入与用户信息")
        self.register_step(
            "risk_check",
            self.risk_check,
            condition=lambda ctx: ctx.input_data.get("amount", 0) > 1000,
            description="大额订单高危风控审计",
        )
        self.register_step("lock_inventory", self.lock_inventory, retries=2, description="锁定仓储库存")
        self.register_step("notify_customer", self.notify_customer, description="异步推送履约通知")

    def validate_order(self, ctx: FlowContext):
        order_id = ctx.input_data.get("order_id")
        amount = ctx.input_data.get("amount", 0)
        if not order_id:
            raise ValueError("缺少 order_id 参数")
        return {"order_id": order_id, "amount": amount, "validated": True}

    def risk_check(self, ctx: FlowContext):
        # 仅对大额订单触发
        return {"risk_passed": True, "score": 98}

    def lock_inventory(self, ctx: FlowContext):
        order_id = ctx.input_data.get("order_id")
        return {"order_id": order_id, "sku_locked": ["SKU-001", "SKU-002"]}

    async def notify_customer(self, ctx: FlowContext):
        # 异步调用演示
        await asyncio.sleep(0.01)
        ctx.output_data["fulfillment_status"] = "SUCCESS"
        ctx.output_data["order_id"] = ctx.input_data.get("order_id")
        return "Notification sent via SMS"


async def main():
    print("=== 初始化 BaseFlow 业务流程编排演示 ===")
    flow = OrderFulfillmentFlow()

    # 注册钩子
    flow.add_step_post_hook(
        lambda sid, sres, ctx: print(f"  [Step Hook] 步骤 '{sid}' 执行完成，耗时: {sres.latency_ms:.2f}ms")
    )

    print("\n--- 1. 执行常规订单流程 (金额 500，跳过大额风控) ---")
    result1 = await flow.execute_async(input_data={"order_id": "ORD_20261029_001", "amount": 500})
    print(f"流程状态: {result1.status.value}, 整体耗时: {result1.total_latency_ms:.2f}ms")
    print(f"执行步骤数: {len(result1.step_results)}")
    print(f"输出数据: {result1.data}")

    print("\n--- 2. 执行大额订单流程 (金额 5000，触发风控步骤) ---")
    result2 = await flow.execute_async(input_data={"order_id": "ORD_20261029_002", "amount": 5000})
    print(f"流程状态: {result2.status.value}, 步骤列表: {[s.step_id for s in result2.step_results]}")

    print("\n--- 3. 查看统计指标度量 (Metrics) ---")
    metrics = flow.get_metrics()
    for k, v in metrics.items():
        print(f"  {k}: {v}")


if __name__ == "__main__":
    asyncio.run(main())
