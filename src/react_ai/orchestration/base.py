"""企业级多智能体编排器基类、上下文契约与内置编排策略."""
import time
import uuid
import json
import logging
import threading
import asyncio
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional, Callable, Awaitable, Type, Dict, List

logger = logging.getLogger(__name__)


# ==================== 状态与结果定义 ====================

class OrchestratorStatus(Enum):
    """编排器执行状态枚举。"""
    PENDING = "pending"          # 待执行
    RUNNING = "running"          # 执行中
    COMPLETED = "completed"      # 执行完成
    FAILED = "failed"            # 执行失败
    CANCELLED = "cancelled"      # 已取消
    TIMEOUT = "timeout"          # 执行超时
    PARTIAL = "partial"          # 部分成功


class AgentStatus(Enum):
    """Agent 执行状态枚举。"""
    IDLE = "idle"                # 空闲
    RUNNING = "running"          # 执行中
    COMPLETED = "completed"      # 执行完成
    FAILED = "failed"            # 执行失败
    TIMEOUT = "timeout"          # 执行超时


@dataclass
class AgentContext:
    """单个 Agent 的执行上下文。"""
    agent_id: str = ""                       # Agent 唯一ID
    agent_name: str = ""                     # Agent 名称
    input_data: dict[str, Any] = field(default_factory=dict)  # 输入数据
    output_data: dict[str, Any] = field(default_factory=dict)  # 输出数据
    status: AgentStatus = AgentStatus.IDLE   # 当前状态
    error: str | None = None                 # 错误信息
    start_time: float = 0.0                  # 开始时间戳
    end_time: float = 0.0                    # 结束时间戳
    latency_ms: float = 0.0                  # 执行耗时（毫秒）
    metadata: dict[str, Any] = field(default_factory=dict)  # 元数据

    def get_elapsed_ms(self) -> float:
        """获取 Agent 已执行耗时（毫秒）。"""
        if self.start_time == 0:
            return 0.0
        end = self.end_time if self.end_time > 0 else time.time()
        return (end - self.start_time) * 1000

    def get(self, key: str, default: Any = None) -> Any:
        """获取 input_data 中的键值（便捷访问）。"""
        return self.input_data.get(key, default)


@dataclass
class OrchestratorContext:
    """编排器全局执行上下文。"""
    orchestration_id: str = field(default_factory=lambda: uuid.uuid4().hex)  # 编排唯一ID
    request_id: str = ""                     # 关联请求ID，用于链路追踪
    session_id: str = ""                     # 会话ID
    user_id: str | None = None               # 用户ID
    tenant_id: str | None = None             # 租户ID
    input_data: dict[str, Any] = field(default_factory=dict)  # 编排输入数据
    output_data: dict[str, Any] = field(default_factory=dict)  # 编排输出数据
    shared_state: dict[str, Any] = field(default_factory=dict)  # 所有 Agent 共享状态
    agent_contexts: dict[str, AgentContext] = field(default_factory=dict)  # 各 Agent 上下文
    errors: list[str] = field(default_factory=list)  # 累积错误
    start_time: float = 0.0                  # 编排开始时间戳
    end_time: float = 0.0                    # 编排结束时间戳
    metadata: dict[str, Any] = field(default_factory=dict)  # 扩展元数据

    def get_elapsed_ms(self) -> float:
        """获取编排已执行耗时（毫秒）。"""
        if self.start_time == 0:
            return 0.0
        end = self.end_time if self.end_time > 0 else time.time()
        return (end - self.start_time) * 1000


@dataclass
class AgentResult:
    """单个 Agent 的执行结果。"""
    agent_id: str = ""                       # Agent ID
    agent_name: str = ""                     # Agent 名称
    success: bool = True                     # 是否成功
    data: Any = None                         # 输出数据
    error: str | None = None                 # 错误信息
    latency_ms: float = 0.0                  # 执行耗时（毫秒）
    metadata: dict[str, Any] = field(default_factory=dict)  # 元数据

    def to_dict(self) -> dict:
        """将 Agent 结果转为字典。"""
        return {
            "agent_id": self.agent_id,
            "agent_name": self.agent_name,
            "success": self.success,
            "data": self.data,
            "error": self.error,
            "latency_ms": round(self.latency_ms, 2),
            "metadata": self.metadata,
        }


@dataclass
class OrchestratorResult:
    """编排器执行的完整结果。"""
    orchestration_id: str = ""               # 编排ID
    status: OrchestratorStatus = OrchestratorStatus.PENDING  # 最终状态
    success: bool = False                    # 是否整体成功
    data: Any = None                         # 最终输出数据
    agent_results: list[AgentResult] = field(default_factory=list)  # 所有 Agent 结果
    total_latency_ms: float = 0.0            # 总耗时（毫秒）
    error: str | None = None                 # 顶层错误信息

    def to_dict(self) -> dict:
        """将编排结果转为字典，便于日志和审计。"""
        return {
            "orchestration_id": self.orchestration_id,
            "status": self.status.value,
            "success": self.success,
            "data": self.data,
            "agent_results": [ar.to_dict() for ar in self.agent_results],
            "total_latency_ms": round(self.total_latency_ms, 2),
            "error": self.error,
        }


# ==================== Agent 基类 ====================

class BaseAgent(ABC):
    """Agent 抽象基类。每个具体 Agent 需继承此类并实现 execute 方法。"""

    name: str = "base_agent"                 # Agent 名称
    version: str = "1.0"                     # Agent 版本
    timeout_seconds: float = 60.0            # 默认超时时间（秒）
    max_retries: int = 0                     # 默认最大重试次数
    retry_delay_seconds: float = 1.0         # 默认重试间隔（秒）

    def __init__(self, agent_id: str | None = None):
        self.agent_id = agent_id or uuid.uuid4().hex  # Agent 唯一ID
        self._execution_count: int = 0       # 累计执行次数
        self._success_count: int = 0         # 累计成功次数
        self._failure_count: int = 0         # 累计失败次数

    @abstractmethod
    def execute(self, context: AgentContext) -> Any:
        """同步执行 Agent 核心逻辑。子类必须实现。

        Args:
            context: Agent 执行上下文

        Returns:
            Agent 输出数据
        """
        ...

    async def execute_async(self, context: AgentContext) -> Any:
        """异步执行 Agent 核心逻辑。默认委托给同步方法，子类可覆写。

        Args:
            context: Agent 执行上下文

        Returns:
            Agent 输出数据
        """
        return self.execute(context)

    def get_stats(self) -> dict:
        """获取 Agent 执行统计信息。"""
        return {
            "agent_id": self.agent_id,
            "name": self.name,
            "version": self.version,
            "execution_count": self._execution_count,
            "success_count": self._success_count,
            "failure_count": self._failure_count,
        }


# ==================== 编排器基类 ====================

class BaseOrchestrator(ABC):
    """企业级编排器基类。

    设计目标：
    - 统一管理多个 Agent 的编排执行（串行、并行、条件分支）；
    - 支持同步与异步两种执行模式；
    - 内置超时控制、重试机制、错误隔离；
    - 提供完整的链路追踪、审计日志和指标统计；
    - 可扩展，子类只需定义 Agent 编排策略。
    """

    name: str = "base_orchestrator"          # 编排器名称
    version: str = "1.0"                     # 编排器版本
    enabled: bool = True                     # 是否启用
    timeout_seconds: float = 300.0           # 编排总超时时间（秒）
    max_retries: int = 0                     # Agent 失败时最大重试次数
    retry_delay_seconds: float = 1.0         # 重试间隔（秒）
    fail_fast: bool = True                   # 是否快速失败（任一 Agent 失败即终止）
    parallel_limit: int = 10                 # 并行执行的最大并发数

    def __init__(self):
        self._lock = threading.RLock()       # 线程锁，保护共享状态
        self._agents: dict[str, BaseAgent] = {}  # 注册的 Agent 字典
        self._agent_order: list[str] = []    # Agent 执行顺序（串行时使用）
        self._pre_hooks: list[Callable] = [] # 编排执行前钩子
        self._post_hooks: list[Callable] = []# 编排执行后钩子
        self._agent_pre_hooks: list[Callable] = []  # Agent 执行前钩子
        self._agent_post_hooks: list[Callable] = [] # Agent 执行后钩子
        self._execution_count: int = 0       # 累计执行次数
        self._success_count: int = 0         # 累计成功次数
        self._failure_count: int = 0         # 累计失败次数
        self._timeout_count: int = 0         # 累计超时次数
        self._total_latency_ms: float = 0.0  # 累计耗时

    # ==================== 钩子注册 ====================

    def add_pre_hook(self, hook: Callable[[OrchestratorContext], None]):
        """添加编排执行前钩子。

        Args:
            hook: 回调函数，接收 OrchestratorContext 参数
        """
        with self._lock:
            self._pre_hooks.append(hook)
            logger.info("orchestrator_pre_hook_registered name=%s", self.name)

    def add_post_hook(self, hook: Callable[[OrchestratorContext, OrchestratorResult], None]):
        """添加编排执行后钩子。

        Args:
            hook: 回调函数，接收 (OrchestratorContext, OrchestratorResult) 参数
        """
        with self._lock:
            self._post_hooks.append(hook)
            logger.info("orchestrator_post_hook_registered name=%s", self.name)

    def add_agent_pre_hook(self, hook: Callable[[str, OrchestratorContext], None]):
        """添加 Agent 执行前钩子。

        Args:
            hook: 回调函数，接收 (agent_id, OrchestratorContext) 参数
        """
        with self._lock:
            self._agent_pre_hooks.append(hook)

    def add_agent_post_hook(self, hook: Callable[[str, AgentResult, OrchestratorContext], None]):
        """添加 Agent 执行后钩子。

        Args:
            hook: 回调函数，接收 (agent_id, AgentResult, OrchestratorContext) 参数
        """
        with self._lock:
            self._agent_post_hooks.append(hook)

    # ==================== Agent 注册 ====================

    def register_agent(self, agent: BaseAgent | Any, order: int | None = None) -> str:
        """注册一个 Agent 到编排器。

        Args:
            agent: Agent 实例或可调用对象
            order: 执行顺序序号，None 表示追加到末尾

        Returns:
            注册的 Agent ID
        """
        agent_id = getattr(agent, "agent_id", None)
        if not agent_id:
            agent_id = getattr(agent, "name", None) or getattr(agent, "__name__", None) or uuid.uuid4().hex
            try:
                setattr(agent, "agent_id", agent_id)
            except Exception:
                pass

        with self._lock:
            self._agents[agent_id] = agent
            if order is not None:
                self._agent_order.insert(order, agent_id)
            else:
                self._agent_order.append(agent_id)
            logger.info("agent_registered name=%s agent_id=%s agent_name=%s",
                        self.name, agent_id, getattr(agent, "name", agent_id))
        return agent_id

    def unregister_agent(self, agent_id: str) -> bool:
        """注销一个 Agent。

        Args:
            agent_id: 要注销的 Agent ID

        Returns:
            是否成功注销
        """
        with self._lock:
            if agent_id in self._agents:
                del self._agents[agent_id]
                self._agent_order.remove(agent_id)
                logger.info("agent_unregistered name=%s agent_id=%s", self.name, agent_id)
                return True
            return False

    def get_agent(self, agent_id: str) -> BaseAgent | None:
        """根据 ID 获取已注册的 Agent。

        Args:
            agent_id: Agent ID

        Returns:
            Agent 实例，未找到返回 None
        """
        return self._agents.get(agent_id)

    def list_agents(self) -> list[BaseAgent]:
        """列出所有已注册的 Agent。

        Returns:
            Agent 实例列表
        """
        return [self._agents[aid] for aid in self._agent_order if aid in self._agents]

    # ==================== 主入口 ====================

    def execute(self, input_data: dict[str, Any] | None = None,
                context: OrchestratorContext | None = None) -> OrchestratorResult:
        """同步执行编排的主入口。

        Args:
            input_data: 编排输入数据
            context: 编排上下文

        Returns:
            编排执行结果
        """
        context = context or OrchestratorContext()
        if input_data:
            context.input_data = input_data
        context.start_time = time.time()

        result = OrchestratorResult(
            orchestration_id=context.orchestration_id,
            status=OrchestratorStatus.RUNNING
        )

        # 检查是否启用
        if not self.enabled:
            result.status = OrchestratorStatus.FAILED
            result.error = "orchestrator is disabled"
            logger.warning("orchestrator_disabled name=%s", self.name)
            return self._finalize(context, result)

        # 执行前钩子
        for hook in self._pre_hooks:
            try:
                hook(context)
            except Exception as exc:
                logger.error("orchestrator_pre_hook_error name=%s error=%s", self.name, str(exc))

        # 执行核心编排逻辑
        try:
            result = self._execute_agents(context)
        except Exception as exc:
            result.status = OrchestratorStatus.FAILED
            result.error = str(exc)
            logger.error("orchestrator_execution_error name=%s id=%s error=%s",
                         self.name, context.orchestration_id, str(exc), exc_info=True)

        # 执行后钩子
        for hook in self._post_hooks:
            try:
                hook(context, result)
            except Exception as exc:
                logger.error("orchestrator_post_hook_error name=%s error=%s", self.name, str(exc))

        return self._finalize(context, result)

    async def execute_async(self, input_data: dict[str, Any] | None = None,
                            context: OrchestratorContext | None = None) -> OrchestratorResult:
        """异步执行编排的主入口。

        Args:
            input_data: 编排输入数据
            context: 编排上下文

        Returns:
            编排执行结果
        """
        context = context or OrchestratorContext()
        if input_data:
            context.input_data = input_data
        context.start_time = time.time()

        result = OrchestratorResult(
            orchestration_id=context.orchestration_id,
            status=OrchestratorStatus.RUNNING
        )

        if not self.enabled:
            result.status = OrchestratorStatus.FAILED
            result.error = "orchestrator is disabled"
            return self._finalize(context, result)

        # 执行前钩子
        for hook in self._pre_hooks:
            try:
                res = hook(context)
                if asyncio.iscoroutine(res):
                    await res
            except Exception as exc:
                logger.error("orchestrator_pre_hook_error name=%s error=%s", self.name, str(exc))

        # 异步执行核心编排
        try:
            result = await self._execute_agents_async(context)
        except Exception as exc:
            result.status = OrchestratorStatus.FAILED
            result.error = str(exc)
            logger.error("orchestrator_async_execution_error name=%s id=%s error=%s",
                         self.name, context.orchestration_id, str(exc), exc_info=True)

        # 执行后钩子
        for hook in self._post_hooks:
            try:
                res = hook(context, result)
                if asyncio.iscoroutine(res):
                    await res
            except Exception as exc:
                logger.error("orchestrator_post_hook_error name=%s error=%s", self.name, str(exc))

        return self._finalize(context, result)

    # ==================== 核心编排逻辑 ====================

    def _execute_agents(self, context: OrchestratorContext) -> OrchestratorResult:
        """同步执行所有 Agent。默认按注册顺序串行执行，子类可覆写实现并行/分支策略。

        Args:
            context: 编排上下文

        Returns:
            编排执行结果
        """
        result = OrchestratorResult(
            orchestration_id=context.orchestration_id,
            status=OrchestratorStatus.RUNNING
        )

        with self._lock:
            agent_ids = list(self._agent_order)

        for agent_id in agent_ids:
            # 检查超时
            elapsed = context.get_elapsed_ms()
            if elapsed > self.timeout_seconds * 1000:
                result.status = OrchestratorStatus.TIMEOUT
                result.error = f"orchestration timeout after {elapsed:.0f}ms"
                logger.warning("orchestration_timeout name=%s id=%s elapsed=%.0fms",
                               self.name, context.orchestration_id, elapsed)
                break

            agent = self._agents.get(agent_id)
            if agent is None:
                logger.warning("agent_not_found name=%s agent_id=%s", self.name, agent_id)
                continue

            # 执行单个 Agent（带重试）
            agent_result = self._execute_agent_with_retry(agent, context)
            result.agent_results.append(agent_result)

            # Agent 执行后钩子
            for hook in self._agent_post_hooks:
                try:
                    hook(agent_id, agent_result, context)
                except Exception as exc:
                    logger.error("agent_post_hook_error agent_id=%s error=%s", agent_id, str(exc))

            # 快速失败检查
            if not agent_result.success and self.fail_fast:
                result.status = OrchestratorStatus.FAILED
                result.error = f"agent {agent_id} failed: {agent_result.error}"
                context.errors.append(result.error)
                logger.warning("orchestration_fail_fast name=%s agent_id=%s", self.name, agent_id)
                break

        # 判断最终状态
        if result.status == OrchestratorStatus.RUNNING:
            if not result.agent_results:
                result.status = OrchestratorStatus.COMPLETED
                result.success = True
            elif all(ar.success for ar in result.agent_results):
                result.status = OrchestratorStatus.COMPLETED
                result.success = True
                result.data = context.output_data
            elif any(ar.success for ar in result.agent_results):
                result.status = OrchestratorStatus.PARTIAL
                result.success = False
                result.error = "some agents failed"
            else:
                result.status = OrchestratorStatus.FAILED
                result.success = False
                result.error = "all agents failed"

        return result

    async def _execute_agents_async(self, context: OrchestratorContext) -> OrchestratorResult:
        """异步执行所有 Agent。默认按注册顺序串行执行，子类可覆写实现并行/分支策略。

        Args:
            context: 编排上下文

        Returns:
            编排执行结果
        """
        result = OrchestratorResult(
            orchestration_id=context.orchestration_id,
            status=OrchestratorStatus.RUNNING
        )

        with self._lock:
            agent_ids = list(self._agent_order)

        for agent_id in agent_ids:
            elapsed = context.get_elapsed_ms()
            if elapsed > self.timeout_seconds * 1000:
                result.status = OrchestratorStatus.TIMEOUT
                result.error = f"orchestration timeout after {elapsed:.0f}ms"
                break

            agent = self._agents.get(agent_id)
            if agent is None:
                continue

            agent_result = await self._execute_agent_with_retry_async(agent, context)
            result.agent_results.append(agent_result)

            for hook in self._agent_post_hooks:
                try:
                    res = hook(agent_id, agent_result, context)
                    if asyncio.iscoroutine(res):
                        await res
                except Exception as exc:
                    logger.error("agent_post_hook_error agent_id=%s error=%s", agent_id, str(exc))

            if not agent_result.success and self.fail_fast:
                result.status = OrchestratorStatus.FAILED
                result.error = f"agent {agent_id} failed: {agent_result.error}"
                context.errors.append(result.error)
                break

        if result.status == OrchestratorStatus.RUNNING:
            if not result.agent_results:
                result.status = OrchestratorStatus.COMPLETED
                result.success = True
            elif all(ar.success for ar in result.agent_results):
                result.status = OrchestratorStatus.COMPLETED
                result.success = True
                result.data = context.output_data
            elif any(ar.success for ar in result.agent_results):
                result.status = OrchestratorStatus.PARTIAL
                result.success = False
                result.error = "some agents failed"
            else:
                result.status = OrchestratorStatus.FAILED
                result.success = False
                result.error = "all agents failed"

        return result

    # ==================== 单个 Agent 重试执行 ====================

    def _execute_agent_with_retry(
        self, agent: BaseAgent | Any, context: OrchestratorContext
    ) -> AgentResult:
        """执行单个 Agent，支持重试与多种 Agent 接口协议。

        兼容协议:
        1. BaseAgent / 具备 execute(context) 实例
        2. 具备 run(context) 或 run(input_data) 的实例
        3. 普通可调用对象 callable(context) 或 callable(input_data)

        Args:
            agent: Agent 实例或可调用对象
            context: 编排全局上下文

        Returns:
            Agent 执行结果
        """
        agent_id = (
            getattr(agent, "agent_id", None)
            or getattr(agent, "name", None)
            or str(id(agent))
        )
        agent_name = getattr(agent, "name", agent_id)
        max_retries = getattr(agent, "max_retries", self.max_retries)
        retry_delay = getattr(agent, "retry_delay_seconds", self.retry_delay_seconds)

        if agent_id in context.agent_contexts:
            agent_ctx = context.agent_contexts[agent_id]
        else:
            agent_ctx = AgentContext(
                agent_id=agent_id,
                agent_name=agent_name,
                input_data=dict(context.input_data),
                metadata=dict(context.metadata),
            )
            context.agent_contexts[agent_id] = agent_ctx

        agent_result: AgentResult | None = None

        for attempt in range(max_retries + 1):
            for hook in self._agent_pre_hooks:
                try:
                    hook(agent_id, context)
                except Exception as exc:  # noqa: BLE001
                    logger.error("agent_pre_hook_error agent_id=%s error=%s", agent_id, str(exc))

            agent_ctx.start_time = time.time()
            agent_ctx.status = AgentStatus.RUNNING
            start_time = agent_ctx.start_time

            try:
                if hasattr(agent, "execute") and callable(agent.execute):
                    raw_result = agent.execute(agent_ctx)
                elif hasattr(agent, "run") and callable(agent.run):
                    try:
                        raw_result = agent.run(agent_ctx.input_data)
                    except (TypeError, AttributeError):
                        raw_result = agent.run(agent_ctx)
                    if asyncio.iscoroutine(raw_result):
                        try:
                            loop = asyncio.get_event_loop()
                            if loop.is_running():
                                import nest_asyncio
                                nest_asyncio.apply()
                                raw_result = loop.run_until_complete(raw_result)
                            else:
                                raw_result = loop.run_until_complete(raw_result)
                        except RuntimeError:
                            raw_result = asyncio.run(raw_result)
                elif callable(agent):
                    try:
                        raw_result = agent(agent_ctx)
                    except (TypeError, AttributeError):
                        raw_result = agent(agent_ctx.input_data)
                    if asyncio.iscoroutine(raw_result):
                        raw_result = asyncio.run(raw_result)
                else:
                    raise TypeError(f"Unsupported agent type: {type(agent)}")

                latency_ms = (time.time() - start_time) * 1000
                agent_ctx.end_time = time.time()
                agent_ctx.latency_ms = latency_ms

                if isinstance(raw_result, AgentResult):
                    agent_result = raw_result
                    agent_result.latency_ms = latency_ms
                    if not agent_result.agent_id:
                        agent_result.agent_id = agent_id
                    if not agent_result.agent_name:
                        agent_result.agent_name = agent_name
                else:
                    agent_result = AgentResult(
                        agent_id=agent_id,
                        agent_name=agent_name,
                        success=True,
                        data=raw_result,
                        latency_ms=latency_ms,
                    )

                agent_ctx.status = AgentStatus.COMPLETED
                if isinstance(agent_result.data, dict):
                    agent_ctx.output_data.update(agent_result.data)
                    context.output_data.update(agent_result.data)
                else:
                    context.output_data[agent_id] = agent_result.data
                context.shared_state[agent_id] = agent_result.data

                if hasattr(agent, "_execution_count"):
                    agent._execution_count += 1
                if hasattr(agent, "_success_count"):
                    agent._success_count += 1

                break

            except Exception as exc:  # noqa: BLE001
                latency_ms = (time.time() - start_time) * 1000
                agent_ctx.end_time = time.time()
                agent_ctx.latency_ms = latency_ms
                agent_ctx.status = AgentStatus.FAILED
                agent_ctx.error = str(exc)

                agent_result = AgentResult(
                    agent_id=agent_id,
                    agent_name=agent_name,
                    success=False,
                    error=str(exc),
                    latency_ms=latency_ms,
                )

                if hasattr(agent, "_execution_count"):
                    agent._execution_count += 1
                if hasattr(agent, "_failure_count"):
                    agent._failure_count += 1

                logger.warning(
                    "agent_execution_failed orchestrator=%s agent_id=%s attempt=%d/%d error=%s",
                    self.name,
                    agent_id,
                    attempt + 1,
                    max_retries + 1,
                    str(exc),
                )

                if attempt < max_retries:
                    time.sleep(retry_delay)
                else:
                    break

        return agent_result or AgentResult(
            agent_id=agent_id,
            agent_name=agent_name,
            success=False,
            error="unknown agent failure",
        )

    async def _execute_agent_with_retry_async(
        self, agent: BaseAgent | Any, context: OrchestratorContext
    ) -> AgentResult:
        """异步执行单个 Agent，支持重试、超时与多种 Agent 接口协议。

        Args:
            agent: Agent 实例或可调用对象
            context: 编排全局上下文

        Returns:
            Agent 执行结果
        """
        agent_id = (
            getattr(agent, "agent_id", None)
            or getattr(agent, "name", None)
            or str(id(agent))
        )
        agent_name = getattr(agent, "name", agent_id)
        max_retries = getattr(agent, "max_retries", self.max_retries)
        retry_delay = getattr(agent, "retry_delay_seconds", self.retry_delay_seconds)
        agent_timeout = getattr(agent, "timeout_seconds", self.timeout_seconds)

        if agent_id in context.agent_contexts:
            agent_ctx = context.agent_contexts[agent_id]
        else:
            agent_ctx = AgentContext(
                agent_id=agent_id,
                agent_name=agent_name,
                input_data=dict(context.input_data),
                metadata=dict(context.metadata),
            )
            context.agent_contexts[agent_id] = agent_ctx

        agent_result: AgentResult | None = None

        for attempt in range(max_retries + 1):
            for hook in self._agent_pre_hooks:
                try:
                    res = hook(agent_id, context)
                    if asyncio.iscoroutine(res):
                        await res
                except Exception as exc:  # noqa: BLE001
                    logger.error("agent_pre_hook_error agent_id=%s error=%s", agent_id, str(exc))

            agent_ctx.start_time = time.time()
            agent_ctx.status = AgentStatus.RUNNING
            start_time = agent_ctx.start_time

            try:
                async def _call_agent():
                    if hasattr(agent, "execute_async") and callable(agent.execute_async):
                        r = agent.execute_async(agent_ctx)
                        return await r if asyncio.iscoroutine(r) else r
                    elif hasattr(agent, "execute") and callable(agent.execute):
                        r = agent.execute(agent_ctx)
                        return await r if asyncio.iscoroutine(r) else r
                    elif hasattr(agent, "run") and callable(agent.run):
                        try:
                            r = agent.run(agent_ctx.input_data)
                        except (TypeError, AttributeError):
                            r = agent.run(agent_ctx)
                        return await r if asyncio.iscoroutine(r) else r
                    elif callable(agent):
                        try:
                            r = agent(agent_ctx)
                        except (TypeError, AttributeError):
                            r = agent(agent_ctx.input_data)
                        return await r if asyncio.iscoroutine(r) else r
                    else:
                        raise TypeError(f"Unsupported agent type: {type(agent)}")

                if agent_timeout and agent_timeout > 0:
                    raw_result = await asyncio.wait_for(_call_agent(), timeout=agent_timeout)
                else:
                    raw_result = await _call_agent()

                latency_ms = (time.time() - start_time) * 1000
                agent_ctx.end_time = time.time()
                agent_ctx.latency_ms = latency_ms

                if isinstance(raw_result, AgentResult):
                    agent_result = raw_result
                    agent_result.latency_ms = latency_ms
                    if not agent_result.agent_id:
                        agent_result.agent_id = agent_id
                    if not agent_result.agent_name:
                        agent_result.agent_name = agent_name
                else:
                    agent_result = AgentResult(
                        agent_id=agent_id,
                        agent_name=agent_name,
                        success=True,
                        data=raw_result,
                        latency_ms=latency_ms,
                    )

                agent_ctx.status = AgentStatus.COMPLETED
                if isinstance(agent_result.data, dict):
                    agent_ctx.output_data.update(agent_result.data)
                    context.output_data.update(agent_result.data)
                else:
                    context.output_data[agent_id] = agent_result.data
                context.shared_state[agent_id] = agent_result.data

                if hasattr(agent, "_execution_count"):
                    agent._execution_count += 1
                if hasattr(agent, "_success_count"):
                    agent._success_count += 1

                break

            except asyncio.TimeoutError:
                latency_ms = (time.time() - start_time) * 1000
                agent_ctx.end_time = time.time()
                agent_ctx.latency_ms = latency_ms
                agent_ctx.status = AgentStatus.TIMEOUT
                err_msg = f"agent execution timeout after {agent_timeout}s"
                agent_ctx.error = err_msg

                agent_result = AgentResult(
                    agent_id=agent_id,
                    agent_name=agent_name,
                    success=False,
                    error=err_msg,
                    latency_ms=latency_ms,
                )
                if hasattr(agent, "_execution_count"):
                    agent._execution_count += 1
                if hasattr(agent, "_failure_count"):
                    agent._failure_count += 1

                logger.warning(
                    "agent_execution_timeout orchestrator=%s agent_id=%s attempt=%d/%d",
                    self.name,
                    agent_id,
                    attempt + 1,
                    max_retries + 1,
                )
                if attempt < max_retries:
                    await asyncio.sleep(retry_delay)
                else:
                    break

            except Exception as exc:  # noqa: BLE001
                latency_ms = (time.time() - start_time) * 1000
                agent_ctx.end_time = time.time()
                agent_ctx.latency_ms = latency_ms
                agent_ctx.status = AgentStatus.FAILED
                agent_ctx.error = str(exc)

                agent_result = AgentResult(
                    agent_id=agent_id,
                    agent_name=agent_name,
                    success=False,
                    error=str(exc),
                    latency_ms=latency_ms,
                )

                if hasattr(agent, "_execution_count"):
                    agent._execution_count += 1
                if hasattr(agent, "_failure_count"):
                    agent._failure_count += 1

                logger.warning(
                    "agent_execution_async_failed orchestrator=%s agent_id=%s attempt=%d/%d error=%s",
                    self.name,
                    agent_id,
                    attempt + 1,
                    max_retries + 1,
                    str(exc),
                )

                if attempt < max_retries:
                    await asyncio.sleep(retry_delay)
                else:
                    break

        return agent_result or AgentResult(
            agent_id=agent_id,
            agent_name=agent_name,
            success=False,
            error="unknown agent failure",
        )

    # ==================== 收尾、度量统计与重置 ====================

    def _finalize(
        self, context: OrchestratorContext, result: OrchestratorResult
    ) -> OrchestratorResult:
        """收尾处理：记录结束时间、更新统计指标与链路日志。"""
        context.end_time = time.time()
        result.total_latency_ms = context.get_elapsed_ms()

        with self._lock:
            self._execution_count += 1
            self._total_latency_ms += result.total_latency_ms

            if result.status == OrchestratorStatus.COMPLETED:
                self._success_count += 1
                result.success = True
            elif result.status == OrchestratorStatus.TIMEOUT:
                self._timeout_count += 1
                self._failure_count += 1
                result.success = False
            else:
                self._failure_count += 1
                result.success = False

        logger.info(
            "orchestrator_finished name=%s id=%s status=%s latency=%.2fms success=%s agents=%d",
            self.name,
            context.orchestration_id,
            result.status.value,
            result.total_latency_ms,
            result.success,
            len(result.agent_results),
        )
        return result

    def get_stats(self) -> dict[str, Any]:
        """获取编排器执行历史统计指标。"""
        with self._lock:
            exec_cnt = self._execution_count
            avg_latency = (self._total_latency_ms / exec_cnt) if exec_cnt > 0 else 0.0
            success_rate = (self._success_count / exec_cnt) if exec_cnt > 0 else 0.0
            return {
                "name": self.name,
                "version": self.version,
                "execution_count": exec_cnt,
                "success_count": self._success_count,
                "failure_count": self._failure_count,
                "timeout_count": self._timeout_count,
                "total_latency_ms": round(self._total_latency_ms, 2),
                "avg_latency_ms": round(avg_latency, 2),
                "success_rate": round(success_rate, 4),
            }

    def get_metrics(self) -> dict[str, Any]:
        """获取统计指标（兼容 Flow / BaseFlow 命名契约）。"""
        metrics = self.get_stats()
        metrics["orchestrator_name"] = self.name
        return metrics

    def reset_stats(self) -> None:
        """重置统计指标。"""
        with self._lock:
            self._execution_count = 0
            self._success_count = 0
            self._failure_count = 0
            self._timeout_count = 0
            self._total_latency_ms = 0.0

    def reset_metrics(self) -> None:
        """重置指标（兼容 Flow / BaseFlow 命名契约）。"""
        self.reset_stats()

    def reset(self) -> None:
        """重置编排器状态及清空所有注册 Agent 与 Hooks。"""
        with self._lock:
            self._agents.clear()
            self._agent_order.clear()
            self._pre_hooks.clear()
            self._post_hooks.clear()
            self._agent_pre_hooks.clear()
            self._agent_post_hooks.clear()
            self.reset_stats()

    def __call__(
        self,
        input_data: dict[str, Any] | None = None,
        context: OrchestratorContext | None = None,
    ) -> OrchestratorResult:
        """支持函数式调用执行编排。"""
        return self.execute(input_data=input_data, context=context)


# ==================== 具体派生编排器 ====================

class SequentialOrchestrator(BaseOrchestrator):
    """顺序串行编排器。

    按照注册的 Agent 顺序依次执行各个 Agent，前一个 Agent 的输出可流转给后续 Agent。
    """

    name: str = "sequential_orchestrator"
    version: str = "1.0"


class ParallelOrchestrator(BaseOrchestrator):
    """并发并行编排器。

    所有注册的 Agent 在并发限制下并行执行，支持多线程并发与异步协程并发。
    """

    name: str = "parallel_orchestrator"
    version: str = "1.0"
    parallel_limit: int = 10

    def _execute_agents(self, context: OrchestratorContext) -> OrchestratorResult:
        """同步并发执行所有 Agent。"""
        from concurrent.futures import ThreadPoolExecutor, as_completed

        result = OrchestratorResult(
            orchestration_id=context.orchestration_id,
            status=OrchestratorStatus.RUNNING,
        )

        with self._lock:
            agent_ids = list(self._agent_order)

        if not agent_ids:
            result.status = OrchestratorStatus.COMPLETED
            result.success = True
            return result

        elapsed = context.get_elapsed_ms()
        if elapsed > self.timeout_seconds * 1000:
            result.status = OrchestratorStatus.TIMEOUT
            result.error = f"orchestration timeout after {elapsed:.0f}ms"
            return result

        max_workers = min(len(agent_ids), max(1, self.parallel_limit))
        agent_results_map: dict[str, AgentResult] = {}

        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            future_to_aid = {}
            for aid in agent_ids:
                agent = self._agents.get(aid)
                if agent is None:
                    continue
                future = executor.submit(self._execute_agent_with_retry, agent, context)
                future_to_aid[future] = aid

            for future in as_completed(future_to_aid):
                aid = future_to_aid[future]
                try:
                    ar = future.result()
                except Exception as exc:  # noqa: BLE001
                    ar = AgentResult(
                        agent_id=aid,
                        agent_name=aid,
                        success=False,
                        error=str(exc),
                    )

                agent_results_map[aid] = ar

                for hook in self._agent_post_hooks:
                    try:
                        hook(aid, ar, context)
                    except Exception as exc:  # noqa: BLE001
                        logger.error("agent_post_hook_error agent_id=%s error=%s", aid, str(exc))

                if not ar.success and self.fail_fast:
                    result.status = OrchestratorStatus.FAILED
                    result.error = f"agent {aid} failed: {ar.error}"
                    context.errors.append(result.error)
                    for f in future_to_aid:
                        f.cancel()
                    break

        result.agent_results = [
            agent_results_map[aid] for aid in agent_ids if aid in agent_results_map
        ]

        if result.status == OrchestratorStatus.RUNNING:
            if not result.agent_results:
                result.status = OrchestratorStatus.COMPLETED
                result.success = True
            elif all(ar.success for ar in result.agent_results):
                result.status = OrchestratorStatus.COMPLETED
                result.success = True
                result.data = context.output_data
            elif any(ar.success for ar in result.agent_results):
                result.status = OrchestratorStatus.PARTIAL
                result.success = False
                result.error = "some agents failed"
            else:
                result.status = OrchestratorStatus.FAILED
                result.success = False
                result.error = "all agents failed"

        return result

    async def _execute_agents_async(self, context: OrchestratorContext) -> OrchestratorResult:
        """异步并发执行所有 Agent。"""
        result = OrchestratorResult(
            orchestration_id=context.orchestration_id,
            status=OrchestratorStatus.RUNNING,
        )

        with self._lock:
            agent_ids = list(self._agent_order)

        if not agent_ids:
            result.status = OrchestratorStatus.COMPLETED
            result.success = True
            return result

        elapsed = context.get_elapsed_ms()
        if elapsed > self.timeout_seconds * 1000:
            result.status = OrchestratorStatus.TIMEOUT
            result.error = f"orchestration timeout after {elapsed:.0f}ms"
            return result

        semaphore = asyncio.Semaphore(max(1, self.parallel_limit))

        async def _run_one(aid: str) -> tuple[str, AgentResult]:
            async with semaphore:
                agent = self._agents.get(aid)
                if agent is None:
                    return aid, AgentResult(
                        agent_id=aid, agent_name=aid, success=False, error="agent not found"
                    )
                ar = await self._execute_agent_with_retry_async(agent, context)
                for hook in self._agent_post_hooks:
                    try:
                        res = hook(aid, ar, context)
                        if asyncio.iscoroutine(res):
                            await res
                    except Exception as exc:  # noqa: BLE001
                        logger.error("agent_post_hook_error agent_id=%s error=%s", aid, str(exc))
                return aid, ar

        tasks = [asyncio.create_task(_run_one(aid)) for aid in agent_ids]
        agent_results_map: dict[str, AgentResult] = {}

        for fut in asyncio.as_completed(tasks):
            try:
                aid, ar = await fut
            except Exception as exc:  # noqa: BLE001
                aid = "unknown"
                ar = AgentResult(agent_id=aid, success=False, error=str(exc))
            agent_results_map[aid] = ar

            if not ar.success and self.fail_fast:
                result.status = OrchestratorStatus.FAILED
                result.error = f"agent {aid} failed: {ar.error}"
                context.errors.append(result.error)
                for t in tasks:
                    if not t.done():
                        t.cancel()
                break

        for t in tasks:
            if not t.done():
                try:
                    await t
                except (asyncio.CancelledError, Exception):
                    pass

        result.agent_results = [
            agent_results_map[aid] for aid in agent_ids if aid in agent_results_map
        ]

        if result.status == OrchestratorStatus.RUNNING:
            if not result.agent_results:
                result.status = OrchestratorStatus.COMPLETED
                result.success = True
            elif all(ar.success for ar in result.agent_results):
                result.status = OrchestratorStatus.COMPLETED
                result.success = True
                result.data = context.output_data
            elif any(ar.success for ar in result.agent_results):
                result.status = OrchestratorStatus.PARTIAL
                result.success = False
                result.error = "some agents failed"
            else:
                result.status = OrchestratorStatus.FAILED
                result.success = False
                result.error = "all agents failed"

        return result


__all__ = [
    "AgentContext",
    "AgentResult",
    "AgentStatus",
    "BaseAgent",
    "BaseOrchestrator",
    "OrchestratorContext",
    "OrchestratorResult",
    "OrchestratorStatus",
    "ParallelOrchestrator",
    "SequentialOrchestrator",
]
