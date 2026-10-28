"""图驱动 / DAG 状态流转执行编排器."""
from typing import Any, Callable, Dict, List


class StateGraphWorkflow:
    def __init__(self):
        self.nodes: Dict[str, Callable] = {}
        self.edges: Dict[str, List[str]] = {}

    def add_node(self, name: str, action: Callable) -> None:
        self.nodes[name] = action

    def add_edge(self, from_node: str, to_node: str) -> None:
        if from_node not in self.edges:
            self.edges[from_node] = []
        self.edges[from_node].append(to_node)

    async def run(self, initial_state: Dict[str, Any]) -> Dict[str, Any]:
        state = dict(initial_state)
        # 执行图拓扑流转
        return state
