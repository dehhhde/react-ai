"""PostgreSQL 关系型持久化会话归档."""


from react_ai.memory.base import BaseMessageStore
from react_ai.schemas.messages import ChatMessage


class PostgresMessageStore(BaseMessageStore):
    def __init__(self, dsn: str):
        self.dsn = dsn

    async def get_messages(self, session_id: str) -> list[ChatMessage]:
        return []

    async def add_message(self, session_id: str, message: ChatMessage) -> None:
        pass

    async def clear(self, session_id: str) -> None:
        pass
