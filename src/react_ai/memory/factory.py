"""配置驱动的记忆装配工厂 (差距分析 2.4).

按 settings.memory.memory_type 决定构建哪种记忆后端，
打通"环境变量配置 → 具体存储实例"的装配链路。
"""

import logging

from react_ai.memory.base import BaseMemory
from react_ai.memory.in_memory import InMemoryMemory

logger = logging.getLogger(__name__)


def create_memory_from_settings() -> BaseMemory:
    """按配置构建会话记忆实例（与审计存储 build_audit_store_from_settings 同款模式）.

    - in_memory（默认）：InMemoryMemory，TTL 受 SESSION_TTL 控制，
      会话历史 token 预算受 HISTORY_TOKEN_BUDGET 控制（差距分析 2.5）；
    - redis：RedisMemory，连接参数取 REDIS_URL 或 REDIS_HOST/PORT/PASSWORD/DB 拼装；
      redis 包缺失或服务不可达时降级内存并告警，不阻断服务启动。
    未知 memory_type 取值告警后按内存实现处理，保持与未配置时行为一致。
    """
    from react_ai.config.settings import settings

    memory_type = (settings.memory.memory_type or "in_memory").lower()
    ttl = float(settings.memory.session_ttl_seconds)
    # 会话历史 token 预算（差距分析 2.5）：<=0 表示不启用裁剪
    token_budget = settings.memory.history_token_budget
    # 租户强制隔离闸（差距分析 2.6）：生产默认 True
    tenant_isolation = settings.memory.enforce_tenant_isolation

    if memory_type == "redis":
        try:
            from react_ai.memory.redis import RedisMemory

            memory = RedisMemory(
                redis_url=settings.memory.resolved_redis_url,
                session_ttl_seconds=ttl,
                history_token_budget=token_budget,
                enforce_tenant_isolation=tenant_isolation,
            )
            if memory.is_connected():
                return memory
            logger.warning(
                "memory backend [redis] unreachable, fallback to in_memory: %s",
                settings.memory.resolved_redis_url,
            )
        except Exception as exc:
            logger.warning(
                "memory backend [redis] unavailable, fallback to in_memory: %s", exc
            )
    elif memory_type != "in_memory":
        logger.warning("unknown memory_type [%s], fallback to in_memory", memory_type)

    return InMemoryMemory(
        name="agent_session_memory",
        session_ttl_seconds=ttl,
        history_token_budget=token_budget,
        enforce_tenant_isolation=tenant_isolation,
    )
