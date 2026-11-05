from react_ai.memory.stores.in_memory import InMemoryMemory, InMemoryMessageStore
from react_ai.memory.stores.postgres import PostgresMessageStore

# Redis 后端为可选依赖：未安装 redis 包时，仅在真正访问对应符号时才触发导入，
# 保证 react_ai.memory 包及依赖它的核心引擎可正常导入。
_LAZY_EXPORTS = {
    "RedisMemory": "react_ai.memory.redis",
    "RedisMessageStore": "react_ai.memory.stores.redis",
}

__all__ = [
    "InMemoryMemory",
    "InMemoryMessageStore",
    "PostgresMessageStore",
    "RedisMemory",
    "RedisMessageStore",
]


def __getattr__(name):
    module_path = _LAZY_EXPORTS.get(name)
    if module_path is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    import importlib

    return getattr(importlib.import_module(module_path), name)


def __dir__():
    return sorted(set(globals()) | set(_LAZY_EXPORTS))
