"""长期语义记忆 (知识召回与用户画像关联)."""




class LongTermSemanticMemory:
    def __init__(self, vector_index_name: str = "agent_memories"):
        self.vector_index_name = vector_index_name

    async def search_relevant_facts(
        self, user_id: str, query: str, top_k: int = 5
    ) -> list[str]:
        # 生产对接向量数据库索引
        return ["用户历史偏好记录: 偏好使用 Python 异步风格代码 (相关度 0.94)"][:top_k]
