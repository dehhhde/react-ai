from react_ai.memory.base import (
    ANONYMOUS_TENANT_ID,
    ANONYMOUS_USER_ID,
    SESSION_HISTORY_KEY,
    TOOL_RESULT_KEY_PREFIX,
    WORKING_STATE_KEY,
    BaseMemory,
    BaseMessageStore,
    MemoryItem,
    MemoryQuery,
    MemoryResult,
    MemoryType,
    SessionKey,
)
from react_ai.memory.factory import create_memory_from_settings
from react_ai.memory.in_memory import InMemoryMemory
from react_ai.memory.long_term import LongTermSemanticMemory
from react_ai.memory.short_term import WindowBufferMemory
from react_ai.memory.stores import (
    InMemoryMessageStore,
    PostgresMessageStore,
)

# Redis 后端为可选依赖：未安装 redis 包时惰性导入，
# 仅在真正访问 RedisMemory / RedisMessageStore 时才报错。
_LAZY_EXPORTS = {
    "RedisMemory": "react_ai.memory.redis",
    "RedisMessageStore": "react_ai.memory.stores.redis",
}

__all__ = [
    "ANONYMOUS_TENANT_ID",
    "ANONYMOUS_USER_ID",
    "SESSION_HISTORY_KEY",
    "TOOL_RESULT_KEY_PREFIX",
    "WORKING_STATE_KEY",
    "BaseMemory",
    "BaseMessageStore",
    "InMemoryMemory",
    "InMemoryMessageStore",
    "LongTermSemanticMemory",
    "MemoryItem",
    "MemoryQuery",
    "MemoryResult",
    "MemoryType",
    "PostgresMessageStore",
    "RedisMemory",
    "RedisMessageStore",
    "SessionKey",
    "WindowBufferMemory",
    "create_memory_from_settings",
]


def __getattr__(name):
    module_path = _LAZY_EXPORTS.get(name)
    if module_path is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    import importlib

    return getattr(importlib.import_module(module_path), name)


def __dir__():
    return sorted(set(globals()) | set(_LAZY_EXPORTS))
