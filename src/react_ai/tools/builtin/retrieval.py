"""知识库检索 (RAG) 工具."""
from typing import Any, Dict, List
from react_ai.tools.base import BaseTool, ToolContext


class KnowledgeRetrievalTool(BaseTool):
    name = "knowledge_search"
    description = "在企业向量知识库中语义搜索相关文档切片"
    side_effects = "read"
    tags = ["rag", "retrieval"]

    @property
    def input_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "检索关键词或问题"},
                "top_k": {"type": "integer", "description": "返回文档切片数量", "minimum": 1, "maximum": 10},
            },
            "required": ["query"],
        }

    def execute(self, arguments: dict, ctx: ToolContext) -> Any:
        query = arguments.get("query", "")
        top_k = arguments.get("top_k", 3)
        return [
            {"score": 0.92, "content": f"关于 [{query}] 的企业知识切片 A: 业务规范定义..."},
            {"score": 0.88, "content": f"关于 [{query}] 的技术手册 B: 接口调用约定..."},
        ][:top_k]
