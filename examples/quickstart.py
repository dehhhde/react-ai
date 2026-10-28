"""快速启动示例 (Quickstart Demo - DeepSeek 驱动，全生命周期事件监听)."""
import sys
from pathlib import Path

# 自动将项目 src 根路径加入模块搜索路径，支持直接运行
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import asyncio
from react_ai.core.engine import ReActAgent
from react_ai.core.context import RunContext
from react_ai.tools.base import tool
from react_ai.tools.registry import ToolRegistry
from react_ai.llm.client import create_llm_client
from react_ai.schemas.events import AgentEventType


@tool(name="calculate", description="计算数学算术表达式结果，入参 expression 为字符串表达式")
def calculate(expression: str) -> str:
    """计算数学表达式."""
    try:
        result = eval(expression, {"__builtins__": {}})
        return str(result)
    except Exception as e:
        return f"计算错误: {e}"


async def main():
    print("=== 初始化 react-ai 企业级 Agent (DeepSeek 真实模型驱动) ===")
    registry = ToolRegistry()
    registry.register(calculate)

    # 默认创建基于 .env 中配置的 DeepSeek 客户端
    llm = create_llm_client()
    agent = ReActAgent(llm=llm, tool_registry=registry)
    ctx = RunContext()

    query = "请帮我计算 (35 * 12) + 180 等于多少？"
    print(f"\n--- 发起流式提问: [{query}] ---")
    
    # 监听并打印全生命周期流式事件
    async for event in agent.stream(query, ctx):
        event_name = event.event_type.value.upper()
        step_tag = f"[Step {event.step}]" if event.step > 0 else "[Global]"

        if event.event_type == AgentEventType.RUN_START:
            print(f"{step_tag} [{event_name}] 开始执行智能体任务，会话ID: {event.payload.get('session_id')}")

        elif event.event_type == AgentEventType.THOUGHT_START:
            print(f"\n{step_tag} [{event_name}] 智能体进入思考阶段 (Thought)，正在决策下一步行动...")

        elif event.event_type == AgentEventType.TOOL_START:
            print(f"{step_tag} [{event_name}] 触发行动 (Action) -> 调用工具: {event.payload.get('tool')} | 入参: {event.payload.get('args')}")

        elif event.event_type == AgentEventType.TOOL_END:
            print(f"{step_tag} [{event_name}] 工具执行完成 -> 耗时: {event.payload.get('duration_ms'):.2f}ms")

        elif event.event_type == AgentEventType.OBSERVATION:
            print(f"{step_tag} [{event_name}] 捕获观察结果 (Observation) -> {event.payload.get('observation')}")

        elif event.event_type == AgentEventType.TOOL_ERROR:
            print(f"{step_tag} [{event_name}] 工具执行异常: {event.payload.get('error')}")

        elif event.event_type == AgentEventType.RUN_END:
            print(f"\n{step_tag} [{event_name}] 任务执行完成，输出最终答复 (Final Answer):\n{event.payload.get('output')}")

        elif event.event_type == AgentEventType.ERROR:
            print(f"\n{step_tag} [{event_name}] 发生错误: {event.payload.get('error')}")

        else:
            print(f"{step_tag} [{event_name}] 事件详情: {event.payload}")


if __name__ == "__main__":
    asyncio.run(main())
