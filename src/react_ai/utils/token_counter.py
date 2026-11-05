"""Token 估算与预算检查."""
from typing import List
from react_ai.schemas.messages import ChatMessage


def estimate_tokens(text: str) -> int:
    # 简易启发式估算 (中英混合): 1 token ≈ 1.5 字符
    if not text:
        return 0
    return int(len(text) / 1.5) + 1


def estimate_messages_tokens(messages: List[ChatMessage]) -> int:
    total = 0
    for msg in messages:
        if msg.content:
            total += estimate_tokens(msg.content)
    return total


def trim_messages_to_token_budget(
    messages: List[ChatMessage], budget: int
) -> tuple[List[ChatMessage], int]:
    """按 token 预算从最旧消息开始丢弃，直到剩余总量不超过预算。

    语义约定 (差距分析 2.5):
    - budget <= 0 视为不启用预算控制，原样返回；
    - 至少保留最新一条消息，即使其单独已超出预算（返回空历史会让
      模型对本轮对话完全失忆，比超限更糟）；
    - 返回 (保留消息列表, 丢弃条数)，保留部分维持原有时间正序。
    """
    if budget <= 0 or not messages:
        return list(messages), 0
    counts = [estimate_tokens(msg.content or "") for msg in messages]
    total = sum(counts)
    dropped = 0
    # 丢弃最旧的，直到进入预算或只剩最后一条
    while total > budget and len(messages) - dropped > 1:
        total -= counts[dropped]
        dropped += 1
    return list(messages[dropped:]), dropped
