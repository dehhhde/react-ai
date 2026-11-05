"""文本向量化实现：OpenAI 兼容 /embeddings 接口 + 零 Key 本地哈希降级。

与 llm/providers/openai.py 同款约定：配置 API Key 时走真实 HTTP 调用，
未配置时降级为确定性哈希伪向量（feature hashing），保证离线开发/演示
端到端可跑。哈希伪向量仅用于本地近似语义，生产必须配置真实 embedding 服务。
"""

import hashlib
import logging
import os
import re
from typing import Any

import httpx

from react_ai.memory.vector.base import BaseEmbeddingProvider

logger = logging.getLogger(__name__)

# OpenAI 兼容 embedding 模型的默认输出维度
_MODEL_DEFAULT_DIMENSIONS: dict[str, int] = {
    "text-embedding-3-small": 1536,
    "text-embedding-3-large": 3072,
    "text-embedding-ada-002": 1536,
}

_WORD_RE = re.compile(r"\w+")


def _text_features(text: str) -> list[str]:
    """提取文本特征：小写词元 + 字符二元组（兼容无空格的中文文本）。"""
    lowered = text.lower()
    features = _WORD_RE.findall(lowered)
    compact = re.sub(r"\s+", "", lowered)
    features.extend(compact[i : i + 2] for i in range(len(compact) - 1))
    return features


class HashingEmbeddingProvider(BaseEmbeddingProvider):
    """确定性哈希伪向量（feature hashing + signed hash trick）。

    无网络依赖、同文本同维度输出恒定，用于零 Key 离线开发与单元测试；
    词面重叠越多的文本余弦相似度越高，可近似"语义相关"排序。
    """

    def __init__(self, dimensions: int = 256):
        if dimensions <= 0:
            raise ValueError("dimensions 必须为正整数")
        self._dimensions = dimensions

    @property
    def dimensions(self) -> int:
        return self._dimensions

    async def embed(self, texts: list[str]) -> list[list[float]]:
        return [self._embed_one(text) for text in texts]

    def _embed_one(self, text: str) -> list[float]:
        vector = [0.0] * self._dimensions
        for feature in _text_features(text or ""):
            digest = hashlib.md5(feature.encode("utf-8")).digest()
            slot = int.from_bytes(digest[:8], "big") % self._dimensions
            sign = 1.0 if digest[8] % 2 == 0 else -1.0
            vector[slot] += sign
        # L2 归一化，使点积即余弦相似度
        norm = sum(v * v for v in vector) ** 0.5
        if norm > 0:
            vector = [v / norm for v in vector]
        return vector


class OpenAIEmbeddingProvider(BaseEmbeddingProvider):
    """OpenAI 兼容协议的真实 embedding 客户端。

    未配置 API Key 时降级为 HashingEmbeddingProvider（与 OpenAIProvider
    的零 Key 离线演示模式一致），并在首次调用时告警一次。
    """

    def __init__(
        self,
        api_key: str | None = None,
        api_base: str | None = None,
        model: str = "text-embedding-3-small",
        dimensions: int | None = None,
        timeout_seconds: float = 30.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ):
        self.api_key = api_key or os.getenv("OPENAI_API_KEY")
        self.api_base = (
            api_base or os.getenv("OPENAI_BASE_URL") or "https://api.openai.com/v1"
        ).rstrip("/")
        self.model = model
        self._dimensions = dimensions or _MODEL_DEFAULT_DIMENSIONS.get(model, 1536)
        self.timeout_seconds = timeout_seconds
        # 测试注入点：可传入 httpx.MockTransport 隔离真实网络
        self._transport = transport
        self._fallback = HashingEmbeddingProvider(dimensions=self._dimensions)

    @property
    def dimensions(self) -> int:
        return self._dimensions

    async def embed(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        if not self.api_key:
            logger.warning(
                "embedding_no_api_key model=%s 降级为本地哈希伪向量，仅适用于离线开发",
                self.model,
            )
            return await self._fallback.embed(texts)
        return await self._call_real_api(texts)

    async def _call_real_api(self, texts: list[str]) -> list[list[float]]:
        url = f"{self.api_base}/embeddings"
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        payload: dict[str, Any] = {"model": self.model, "input": texts}
        # text-embedding-3-* 系列支持 Matryoshka 维度截断
        if self.model.startswith("text-embedding-3-"):
            payload["dimensions"] = self._dimensions

        async with httpx.AsyncClient(
            timeout=self.timeout_seconds, transport=self._transport
        ) as client:
            resp = await client.post(url, headers=headers, json=payload)
            resp.raise_for_status()
            data = resp.json()

        rows = sorted(data.get("data", []), key=lambda d: d.get("index", 0))
        vectors = [row["embedding"] for row in rows]
        if len(vectors) != len(texts):
            raise RuntimeError(
                f"embedding 响应数量不匹配: request={len(texts)} response={len(vectors)}"
            )
        return vectors

    def __repr__(self) -> str:  # 避免 Key 泄漏到日志
        return (
            f"OpenAIEmbeddingProvider(model={self.model!r}, "
            f"api_base={self.api_base!r}, dimensions={self._dimensions}, "
            f"configured={bool(self.api_key)})"
        )
