"""本地易失性内存消息存储 (开发与单元测试)."""


from react_ai.memory.base import BaseMessageStore
from react_ai.memory.in_memory import InMemoryMemory
from react_ai.schemas.messages import ChatMessage


class InMemoryMessageStore(BaseMessageStore):
    def __init__(self):
        self._storage: dict[str, list[ChatMessage]] = {}

    async def get_messages(self, session_id: str) -> list[ChatMessage]:
        return list(self._storage.get(session_id, []))

    async def add_message(self, session_id: str, message: ChatMessage) -> None:
        if session_id not in self._storage:
            self._storage[session_id] = []
        self._storage[session_id].append(message)

    async def clear(self, session_id: str) -> None:
        self._storage.pop(session_id, None)


__all__ = ["InMemoryMemory", "InMemoryMessageStore"]
