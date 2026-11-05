"""统一 Jinja2 渲染内核：BasePrompt 与 PromptManager 共用同一 Environment.

对应差距分析 2.2（渲染路径统一）与 2.12（渲染缓存）：
- 单一 Jinja2 Environment，StrictUndefined 语义，模板缺变量即报错而非静默空串；
- 按模板源码哈希缓存编译产物，高并发下复用，提供命中率指标；
- 内置 `mask_pii` 自定义过滤器（truncate 使用 Jinja 原生过滤器）。
"""
from __future__ import annotations

import hashlib
import threading
from typing import Any

from jinja2 import Environment, StrictUndefined, Template

_PII_PATTERNS = (
    (r"\d{17}[\dXx]", "[ID_MASKED]"),
    (r"1[3-9]\d{9}", "[PHONE_MASKED]"),
)


def mask_pii(value: Any) -> str:
    """Jinja 过滤器：对手机号/身份证号做掩码脱敏."""
    import re

    text = str(value)
    text = re.sub(_PII_PATTERNS[0][0], _PII_PATTERNS[0][1], text)
    text = re.sub(_PII_PATTERNS[1][0], _PII_PATTERNS[1][1], text)
    return text


class RenderCacheStats:
    """模板编译缓存命中统计."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.hits = 0
        self.misses = 0

    def hit(self) -> None:
        with self._lock:
            self.hits += 1

    def miss(self) -> None:
        with self._lock:
            self.misses += 1

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            total = self.hits + self.misses
            return {
                "hits": self.hits,
                "misses": self.misses,
                "hit_rate": round(self.hits / total, 4) if total else 0.0,
            }

    def reset(self) -> None:
        with self._lock:
            self.hits = 0
            self.misses = 0


# 全局共享 Environment：StrictUndefined + 自动转义关闭（提示词非 HTML 场景）
PROMPT_ENV = Environment(
    undefined=StrictUndefined,
    autoescape=False,
    trim_blocks=True,
    lstrip_blocks=True,
    keep_trailing_newline=False,
)
PROMPT_ENV.filters["mask_pii"] = mask_pii

_cache: dict[str, Template] = {}
_cache_lock = threading.Lock()
cache_stats = RenderCacheStats()


def render_template(source: str, variables: dict[str, Any] | None = None) -> str:
    """用共享内核渲染模板字符串：同名模板第二次渲染复用编译产物.

    缺变量时抛 jinja2.UndefinedError（StrictUndefined），由调用方转业务异常。
    """
    key = hashlib.sha256(source.encode("utf-8")).hexdigest()
    with _cache_lock:
        tpl = _cache.get(key)
    if tpl is None:
        cache_stats.miss()
        tpl = PROMPT_ENV.from_string(source)
        with _cache_lock:
            _cache[key] = tpl
    else:
        cache_stats.hit()
    return tpl.render(**(variables or {}))


def cache_snapshot() -> dict[str, Any]:
    """缓存命中率与规模指标."""
    snap = cache_stats.snapshot()
    with _cache_lock:
        snap["compiled_templates"] = len(_cache)
    return snap
