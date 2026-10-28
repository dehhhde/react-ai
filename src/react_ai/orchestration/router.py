"""意图分类与专业智能体路由分发器."""
from typing import Dict
from react_ai.core.base import BaseAgent


class AgentRouter:
    def __init__(self):
        self._route_table: Dict[str, BaseAgent] = {}

    def register_route(self, intent_category: str, agent: BaseAgent) -> None:
        self._route_table[intent_category] = agent

    def route(self, query: str) -> BaseAgent:
        # 基于意图匹配或轻量 LLM 决策
        return list(self._route_table.values())[0] if self._route_table else None
