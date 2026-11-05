"""向量检索抽象层：Embedding 提供方与向量存储的统一接口（差距分析 2.3）。

设计目标：
- 长期语义记忆的写入侧（事实抽取 → embedding → upsert）与检索侧
  （query embedding → ANN top-k）都只依赖本模块抽象，可替换实现；
- 向量记录携带 user_id/tenant_id 身份维度，存储层强制按身份过滤，
  保证"无关用户不可召回"的隔离验收标准；
- 所有接口为异步，同步驱动（如 psycopg）实现须自行用 asyncio.to_thread 卸载。
"""

import math
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any


@dataclass
class VectorRecord:
    """单条向量记录：待召回文本 + embedding 向量 + 身份维度。"""

    id: str  # 记录唯一 ID
    text: str  # 原始文本（召回后直接返回给调用方）
    vector: list[float]  # 文本的 embedding 向量
    user_id: str | None = None  # 所属用户，检索时强制过滤
    tenant_id: str | None = None  # 所属租户，检索时强制过滤
    metadata: dict[str, Any] = field(default_factory=dict)  # 扩展元数据
    created_at: float = 0.0  # 写入时间戳


@dataclass
class SearchHit:
    """一次向量检索命中：记录 + 相似度得分（越大越相似）。"""

    record: VectorRecord
    score: float


class BaseEmbeddingProvider(ABC):
    """文本向量化提供方抽象。"""

    @property
    @abstractmethod
    def dimensions(self) -> int:
        """向量维度，向量索引创建时依赖该值。"""

    @abstractmethod
    async def embed(self, texts: list[str]) -> list[list[float]]:
        """批量将文本转为向量，返回顺序与输入一致。"""


class BaseVectorStore(ABC):
    """向量存储抽象：按索引名（如 pgvector 表名）隔离不同用途的向量集。"""

    async def ensure_index(self, index: str, dimensions: int) -> None:
        """确保索引存在且维度匹配；默认空实现，外部存储子类应覆写。"""
        return None

    @abstractmethod
    async def upsert(self, index: str, records: list[VectorRecord]) -> None:
        """写入或按 ID 覆盖向量记录。"""

    @abstractmethod
    async def search(
        self,
        index: str,
        query_vector: list[float],
        top_k: int = 5,
        *,
        user_id: str | None = None,
        tenant_id: str | None = None,
    ) -> list[SearchHit]:
        """ANN 检索：返回相似度降序的 top_k 命中。

        传入 user_id/tenant_id 时存储层必须强制过滤，未命中的身份维度
        记录不可见；两者均为 None 表示不限定（仅限受控的内部调用）。
        """

    @abstractmethod
    async def delete_by_user(
        self, index: str, user_id: str, *, tenant_id: str | None = None
    ) -> int:
        """删除某用户（可选限定租户）的全部向量记录，返回删除数量。"""


def cosine_similarity(a: list[float], b: list[float]) -> float:
    """余弦相似度，供内存实现与单测复用；任一向量为零向量时返回 0。"""
    dot = 0.0
    norm_a = 0.0
    norm_b = 0.0
    for x, y in zip(a, b):
        dot += x * y
        norm_a += x * x
        norm_b += y * y
    if norm_a <= 0.0 or norm_b <= 0.0:
        return 0.0
    return dot / math.sqrt(norm_a * norm_b)
