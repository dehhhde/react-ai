"""向量检索子包：长期语义记忆（差距分析 2.3）的 embedding 与向量存储基建。"""

from react_ai.memory.vector.base import (
    BaseEmbeddingProvider,
    BaseVectorStore,
    SearchHit,
    VectorRecord,
    cosine_similarity,
)
from react_ai.memory.vector.embedding import (
    HashingEmbeddingProvider,
    OpenAIEmbeddingProvider,
)
from react_ai.memory.vector.in_memory import InMemoryVectorStore
from react_ai.memory.vector.pgvector import PgVectorStore

__all__ = [
    "BaseEmbeddingProvider",
    "BaseVectorStore",
    "HashingEmbeddingProvider",
    "InMemoryVectorStore",
    "OpenAIEmbeddingProvider",
    "PgVectorStore",
    "SearchHit",
    "VectorRecord",
    "cosine_similarity",
]
