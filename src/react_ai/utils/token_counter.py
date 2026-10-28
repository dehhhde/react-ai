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
