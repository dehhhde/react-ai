"""Redis 分布式会话缓存存储 (支持自动过期 TTL 与多维索引记忆)."""


from react_ai.memory.base import BaseMessageStore
from react_ai.memory.redis import RedisMemory
from react_ai.schemas.messages import ChatMessage


class RedisMessageStore(BaseMessageStore):
    def __init__(self, redis_url: str = "redis://localhost:6379/0", ttl: int = 86400):
        self.redis_url = redis_url
        self.ttl = ttl

    async def get_messages(self, session_id: str) -> list[ChatMessage]:
        return []

    async def add_message(self, session_id: str, message: ChatMessage) -> None:
        pass

    async def clear(self, session_id: str) -> None:
        pass


__all__ = ["RedisMemory", "RedisMessageStore"]
