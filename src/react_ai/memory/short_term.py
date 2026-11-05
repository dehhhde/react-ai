"""短期工作记忆 (滑动窗口 + 摘要压缩)."""


from react_ai.memory.base import BaseMessageStore
from react_ai.memory.stores.in_memory import InMemoryMessageStore
from react_ai.schemas.messages import ChatMessage, MessageRole
from react_ai.utils.token_counter import trim_messages_to_token_budget


class WindowBufferMemory:
    """会话短期滑动窗口记忆。

    两级裁剪（差距分析 2.5）：先按条数保留最近 2*k 条，再按 token 预算
    从最旧消息开始丢弃。token_budget 为 None/<=0 时仅按条数截断，
    行为与改造前一致。
    """

    def __init__(
        self,
        k: int = 10,
        store: BaseMessageStore | None = None,
        token_budget: int | None = None,
    ):
        self.k = k
        self.store = store or InMemoryMessageStore()
        self.token_budget = token_budget

    async def load_memory_variables(self, session_id: str) -> list[ChatMessage]:
        all_msgs = await self.store.get_messages(session_id)
        windowed = all_msgs[-(2 * self.k) :]
        budget = self.token_budget
        if budget is not None and budget > 0:
            windowed, _ = trim_messages_to_token_budget(windowed, budget)
        return windowed

    async def save_context(
        self, session_id: str, human_input: str, ai_output: str
    ) -> None:
        await self.store.add_message(
            session_id, ChatMessage(role=MessageRole.USER, content=human_input)
        )
        await self.store.add_message(
            session_id, ChatMessage(role=MessageRole.ASSISTANT, content=ai_output)
        )
