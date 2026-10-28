"""主管-员工模式 (Hierarchical Supervisor-Worker Pattern)."""
from typing import Dict, List
from react_ai.core.base import BaseAgent
from react_ai.core.context import RunContext
from react_ai.schemas.agent import AgentResponse


class SupervisorAgent:
    def __init__(self, supervisor_llm, workers: Dict[str, BaseAgent]):
        self.supervisor_llm = supervisor_llm
        self.workers = workers

    async def execute_task(self, main_task: str, context: RunContext) -> AgentResponse:
        # 1. 拆解子任务
        # 2. 分发给对应 worker 并发或串行执行
        # 3. 汇总 worker 成果输出报告
        return AgentResponse(
            session_id=context.session_id,
            run_id=context.run_id,
            final_output=f"Supervisor 任务完成: {main_task}",
        )
