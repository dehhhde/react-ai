"""短期工作记忆 (滑动窗口 + 摘要压缩)."""


from react_ai.memory.base import BaseMessageStore
from react_ai.memory.stores.in_memory import InMemoryMessageStore
from react_ai.schemas.messages import ChatMessage, MessageRole


class WindowBufferMemory:
    """会话短期滑动窗口记忆。"""

    def __init__(self, k: int = 10, store: BaseMessageStore | None = None):
        self.k = k
        self.store = store or InMemoryMessageStore()

    async def load_memory_variables(self, session_id: str) -> list[ChatMessage]:
        all_msgs = await self.store.get_messages(session_id)
        return all_msgs[-(2 * self.k) :]

    async def save_context(
        self, session_id: str, human_input: str, ai_output: str
    ) -> None:
        await self.store.add_message(
            session_id, ChatMessage(role=MessageRole.USER, content=human_input)
        )
        await self.store.add_message(
            session_id, ChatMessage(role=MessageRole.ASSISTANT, content=ai_output)
        )
