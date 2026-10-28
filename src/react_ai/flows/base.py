"""企业级流程编排基类、执行上下文与结果契约."""
import asyncio
import logging
import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

logger = logging.getLogger(__name__)


class FlowStatus(Enum):
    """流程执行状态枚举。"""
    PENDING = "pending"          # 待执行
    RUNNING = "running"          # 执行中
    COMPLETED = "completed"      # 执行完成
    FAILED = "failed"            # 执行失败
    CANCELLED = "cancelled"      # 已取消
    TIMEOUT = "timeout"          # 执行超时


class FlowPriority(Enum):
    """流程优先级枚举。"""
    LOW = 0       # 低优先级
    MEDIUM = 1    # 中优先级
    HIGH = 2      # 高优先级
    CRITICAL = 3  # 最高优先级


@dataclass
class FlowContext:
    """流程执行上下文，贯穿整个流程生命周期。"""
    flow_id: str = field(default_factory=lambda: uuid.uuid4().hex)  # 流程唯一ID
    request_id: str = ""                     # 关联的请求ID，用于链路追踪
    session_id: str = ""                     # 会话ID
    user_id: str | None = None               # 用户ID
    tenant_id: str | None = None             # 租户ID
    metadata: dict[str, Any] = field(default_factory=dict)  # 扩展元数据
    input_data: dict[str, Any] = field(default_factory=dict)  # 流程输入数据
    output_data: dict[str, Any] = field(default_factory=dict)  # 流程输出数据
    shared_state: dict[str, Any] = field(default_factory=dict)  # 各步骤间共享状态
    errors: list[str] = field(default_factory=list)  # 累积的错误信息
    start_time: float = 0.0                  # 流程开始时间戳
    end_time: float = 0.0                    # 流程结束时间戳

    def get_elapsed_ms(self) -> float:
        """获取流程已执行耗时（毫秒）。"""
        if self.start_time == 0:
            return 0.0  # 尚未开始
        end = self.end_time if self.end_time > 0 else time.time()  # 取结束时间或当前时间
        return (end - self.start_time) * 1000  # 转为毫秒

    def to_dict(self) -> dict[str, Any]:
        """将上下文关键信息转为字典。"""
        return {
            "flow_id": self.flow_id,
            "request_id": self.request_id,
            "session_id": self.session_id,
            "user_id": self.user_id,
            "tenant_id": self.tenant_id,
            "metadata": self.metadata,
            "input_data": self.input_data,
            "output_data": self.output_data,
            "shared_state": self.shared_state,
            "errors": self.errors,
            "start_time": self.start_time,
            "end_time": self.end_time,
            "elapsed_ms": round(self.get_elapsed_ms(), 2),
        }


@dataclass
class StepResult:
    """单个流程步骤的执行结果。"""
    step_id: str = ""                        # 步骤ID
    step_name: str = ""                      # 步骤名称
    success: bool = True                     # 是否执行成功
    data: Any = None                         # 步骤输出数据
    error: str | None = None                 # 错误信息（失败时有效）
    latency_ms: float = 0.0                  # 步骤执行耗时（毫秒）
    metadata: dict[str, Any] = field(default_factory=dict)  # 步骤级元数据

    def to_dict(self) -> dict[str, Any]:
        """将步骤结果转为字典。"""
        return {
            "step_id": self.step_id,
            "step_name": self.step_name,
            "success": self.success,
            "data": self.data,
            "error": self.error,
            "latency_ms": round(self.latency_ms, 2),
            "metadata": self.metadata,
        }


@dataclass
class FlowResult:
    """流程执行的完整结果。"""
    flow_id: str = ""                        # 流程ID
    status: FlowStatus = FlowStatus.PENDING  # 最终状态
    success: bool = False                    # 是否整体成功
    data: Any = None                         # 流程最终输出数据
    step_results: list[StepResult] = field(default_factory=list)  # 所有步骤结果
    total_latency_ms: float = 0.0            # 总耗时（毫秒）
    error: str | None = None                 # 顶层错误信息

    def to_dict(self) -> dict[str, Any]:
        """将流程结果转为字典，便于日志和审计。"""
        status_val = self.status.value if isinstance(self.status, FlowStatus) else str(self.status)
        return {
            "flow_id": self.flow_id,
            "status": status_val,
            "success": self.success,
            "data": self.data,
            "step_results": [sr.to_dict() for sr in self.step_results],
            "total_latency_ms": round(self.total_latency_ms, 2),
            "error": self.error,
        }


class BaseFlow:
    """企业级流程编排基类。

    设计目标：
    - 提供统一的流程生命周期管理（初始化 → 执行 → 完成/失败）；
    - 支持同步与异步两种执行模式；
    - 内置步骤编排、错误处理、超时控制、重试机制；
    - 提供完整的链路追踪、审计日志和指标统计；
    - 可扩展，子类只需实现核心步骤逻辑。
    """

    name: str = "base_flow"                  # 流程名称，用于日志和标识
    version: str = "1.0"                     # 流程版本
    enabled: bool = True                     # 是否启用该流程
    priority: FlowPriority = FlowPriority.MEDIUM  # 默认优先级
    timeout_seconds: float = 300.0           # 流程总超时时间（秒）
    max_retries: int = 0                     # 步骤失败时的最大重试次数
    retry_delay_seconds: float = 1.0         # 重试间隔时间（秒）
    short_circuit_on_error: bool = True      # 某步骤失败时是否短路（不再执行后续步骤）

    def __init__(self):
        self._lock = threading.RLock()       # 线程锁，保护共享状态
        self._execution_count: int = 0       # 累计执行次数
        self._success_count: int = 0         # 累计成功次数
        self._failure_count: int = 0         # 累计失败次数
        self._timeout_count: int = 0         # 累计超时次数
        self._total_latency_ms: float = 0.0  # 累计耗时（毫秒）
        self._pre_hooks: list[Callable] = [] # 流程执行前钩子
        self._post_hooks: list[Callable] = []# 流程执行后钩子
        self._step_pre_hooks: list[Callable] = []  # 步骤执行前钩子
        self._step_post_hooks: list[Callable] = [] # 步骤执行后钩子
        self._steps: list[dict[str, Any]] = []     # 注册的步骤列表
        self.build_steps()                   # 子类可重写此方法以自动注册步骤

    def build_steps(self) -> None:
        """步骤构建钩子，子类可覆盖此方法以在初始化时注册步骤。"""

    @property
    def steps(self) -> list[dict[str, Any]]:
        """获取已注册步骤的快照列表。"""
        with self._lock:
            return list(self._steps)

    # ==================== 钩子注册 ====================

    def add_pre_hook(self, hook: Callable[[FlowContext], None]):
        """添加流程执行前钩子。

        Args:
            hook: 回调函数，接收 FlowContext 参数
        """
        with self._lock:
            self._pre_hooks.append(hook)
            logger.info("flow_pre_hook_registered flow=%s", self.name)

    def add_post_hook(self, hook: Callable[[FlowContext, FlowResult], None]):
        """添加流程执行后钩子。

        Args:
            hook: 回调函数，接收 (FlowContext, FlowResult) 参数
        """
        with self._lock:
            self._post_hooks.append(hook)
            logger.info("flow_post_hook_registered flow=%s", self.name)

    def add_step_pre_hook(self, hook: Callable[[str, FlowContext], None]):
        """添加步骤执行前钩子。

        Args:
            hook: 回调函数，接收 (step_id, FlowContext) 参数
        """
        with self._lock:
            self._step_pre_hooks.append(hook)

    def add_step_post_hook(self, hook: Callable[[str, StepResult, FlowContext], None]):
        """添加步骤执行后钩子。

        Args:
            hook: 回调函数，接收 (step_id, StepResult, FlowContext) 参数
        """
        with self._lock:
            self._step_post_hooks.append(hook)

    # ==================== 步骤注册 ====================

    def register_step(self, step_id: str, step_func: Callable, **kwargs):
        """注册一个流程步骤。

        Args:
            step_id: 步骤唯一ID
            step_func: 步骤执行函数，接收 (FlowContext) 参数，返回 StepResult 或 Any
            **kwargs: 额外配置，如 timeout, retries, condition, description 等
        """
        step_config = {
            "step_id": step_id,
            "step_func": step_func,
            "timeout": kwargs.get("timeout", self.timeout_seconds),   # 步骤级超时
            "retries": kwargs.get("retries", self.max_retries),       # 步骤级重试次数
            "condition": kwargs.get("condition", None),               # 执行条件函数
            "description": kwargs.get("description", ""),             # 步骤描述
        }
        with self._lock:
            self._steps.append(step_config)
            logger.info("flow_step_registered flow=%s step_id=%s", self.name, step_id)

    def unregister_step(self, step_id: str) -> bool:
        """注销一个流程步骤。

        Args:
            step_id: 要注销的步骤ID

        Returns:
            是否成功注销
        """
        with self._lock:
            for i, step in enumerate(self._steps):
                if step["step_id"] == step_id:
                    self._steps.pop(i)
                    logger.info("flow_step_unregistered flow=%s step_id=%s", self.name, step_id)
                    return True
            return False

    # ==================== 主入口 ====================

    def execute(self, input_data: dict[str, Any] | None = None,
                context: FlowContext | None = None) -> FlowResult:
        """同步执行流程的主入口。

        Args:
            input_data: 流程输入数据
            context: 流程上下文，可传入已有上下文或新建

        Returns:
            流程执行结果
        """
        context = context or FlowContext()     # 使用传入上下文或新建
        if input_data:
            context.input_data = input_data    # 设置输入数据
        context.start_time = time.time()       # 记录开始时间

        result = FlowResult(flow_id=context.flow_id, status=FlowStatus.RUNNING)

        # 检查是否启用
        if not self.enabled:
            result.status = FlowStatus.FAILED
            result.error = "flow is disabled"
            logger.warning("flow_disabled name=%s flow_id=%s", self.name, context.flow_id)
            return self._finalize(context, result)

        # 执行流程前钩子
        for hook in self._pre_hooks:
            try:
                hook(context)                  # 调用钩子函数
            except Exception as exc:  # noqa: BLE001
                logger.error("flow_pre_hook_error flow=%s error=%s", self.name, str(exc))

        # 执行核心流程
        try:
            result = self._execute_steps(context)
        except Exception as exc:
            result.status = FlowStatus.FAILED
            result.error = str(exc)
            logger.exception("flow_execution_error flow=%s flow_id=%s", self.name, context.flow_id)

        # 执行流程后钩子
        for hook in self._post_hooks:
            try:
                hook(context, result)          # 调用钩子函数
            except Exception as exc:  # noqa: BLE001
                logger.error("flow_post_hook_error flow=%s error=%s", self.name, str(exc))

        return self._finalize(context, result)

    async def execute_async(self, input_data: dict[str, Any] | None = None,
                            context: FlowContext | None = None) -> FlowResult:
        """异步执行流程的主入口。

        Args:
            input_data: 流程输入数据
            context: 流程上下文

        Returns:
            流程执行结果
        """
        context = context or FlowContext()     # 使用传入上下文或新建
        if input_data:
            context.input_data = input_data    # 设置输入数据
        context.start_time = time.time()       # 记录开始时间

        result = FlowResult(flow_id=context.flow_id, status=FlowStatus.RUNNING)

        # 检查是否启用
        if not self.enabled:
            result.status = FlowStatus.FAILED
            result.error = "flow is disabled"
            return self._finalize(context, result)

        # 执行流程前钩子（兼容异步与同步钩子）
        for hook in self._pre_hooks:
            try:
                res = hook(context)
                if asyncio.iscoroutine(res):
                    await res
            except Exception as exc:  # noqa: BLE001
                logger.error("flow_pre_hook_error flow=%s error=%s", self.name, str(exc))

        # 异步执行核心流程
        try:
            result = await self._execute_steps_async(context)
        except Exception as exc:
            result.status = FlowStatus.FAILED
            result.error = str(exc)
            logger.exception("flow_async_execution_error flow=%s flow_id=%s", self.name, context.flow_id)

        # 执行流程后钩子
        for hook in self._post_hooks:
            try:
                res = hook(context, result)
                if asyncio.iscoroutine(res):
                    await res
            except Exception as exc:  # noqa: BLE001
                logger.error("flow_post_hook_error flow=%s error=%s", self.name, str(exc))

        return self._finalize(context, result)

    def __call__(self, input_data: dict[str, Any] | None = None,
                 context: FlowContext | None = None) -> FlowResult:
        """同步可调用入口快捷方式。"""
        return self.execute(input_data=input_data, context=context)

    # ==================== 核心执行逻辑 ====================

    def _execute_steps(self, context: FlowContext) -> FlowResult:
        """同步执行所有注册的步骤。

        Args:
            context: 流程上下文

        Returns:
            流程执行结果
        """
        result = FlowResult(flow_id=context.flow_id, status=FlowStatus.RUNNING)

        with self._lock:
            steps_snapshot = list(self._steps)  # 快照，避免执行中步骤被修改

        for step_config in steps_snapshot:
            # 检查是否已超时
            elapsed = context.get_elapsed_ms()
            if elapsed > self.timeout_seconds * 1000:
                result.status = FlowStatus.TIMEOUT
                result.error = f"flow timeout after {elapsed:.0f}ms"
                logger.warning("flow_timeout flow=%s flow_id=%s elapsed=%.0fms",
                               self.name, context.flow_id, elapsed)
                break

            # 检查步骤执行条件
            condition = step_config.get("condition")
            if condition and not condition(context):
                logger.info("flow_step_skipped flow=%s step_id=%s reason=condition_not_met",
                            self.name, step_config["step_id"])
                continue

            # 执行单个步骤（带重试）
            step_result = self._execute_step_with_retry(step_config, context)
            result.step_results.append(step_result)

            # 步骤执行后钩子
            for hook in self._step_post_hooks:
                try:
                    hook(step_config["step_id"], step_result, context)
                except Exception as exc:  # noqa: BLE001
                    logger.error("flow_step_post_hook_error step_id=%s error=%s",
                                 step_config["step_id"], str(exc))

            # 如果步骤失败且配置了短路，则终止
            if not step_result.success and self.short_circuit_on_error:
                result.status = FlowStatus.FAILED
                result.error = f"step {step_config['step_id']} failed: {step_result.error}"
                context.errors.append(result.error)
                logger.warning("flow_short_circuit flow=%s step_id=%s",
                               self.name, step_config["step_id"])
                break

        # 所有步骤执行完毕，判断最终状态
        all_success = all(sr.success for sr in result.step_results) if result.step_results else True
        if result.status == FlowStatus.RUNNING and all_success:
            result.status = FlowStatus.COMPLETED
            result.success = True
            result.data = context.output_data

        return result

    async def _execute_steps_async(self, context: FlowContext) -> FlowResult:
        """异步执行所有注册的步骤。

        Args:
            context: 流程上下文

        Returns:
            流程执行结果
        """
        result = FlowResult(flow_id=context.flow_id, status=FlowStatus.RUNNING)

        with self._lock:
            steps_snapshot = list(self._steps)

        for step_config in steps_snapshot:
            # 检查超时
            elapsed = context.get_elapsed_ms()
            if elapsed > self.timeout_seconds * 1000:
                result.status = FlowStatus.TIMEOUT
                result.error = f"flow timeout after {elapsed:.0f}ms"
                logger.warning("flow_timeout flow=%s flow_id=%s elapsed=%.0fms",
                               self.name, context.flow_id, elapsed)
                break

            # 检查条件
            condition = step_config.get("condition")
            if condition:
                cond_val = condition(context)
                if asyncio.iscoroutine(cond_val):
                    cond_val = await cond_val
                if not cond_val:
                    logger.info("flow_step_skipped flow=%s step_id=%s reason=condition_not_met",
                                self.name, step_config["step_id"])
                    continue

            # 异步执行步骤（带重试）
            step_result = await self._execute_step_with_retry_async(step_config, context)
            result.step_results.append(step_result)

            # 步骤执行后钩子
            for hook in self._step_post_hooks:
                try:
                    res = hook(step_config["step_id"], step_result, context)
                    if asyncio.iscoroutine(res):
                        await res
                except Exception as exc:  # noqa: BLE001
                    logger.error("flow_step_post_hook_error step_id=%s error=%s",
                                 step_config["step_id"], str(exc))

            # 短路检查
            if not step_result.success and self.short_circuit_on_error:
                result.status = FlowStatus.FAILED
                result.error = f"step {step_config['step_id']} failed: {step_result.error}"
                context.errors.append(result.error)
                logger.warning("flow_short_circuit flow=%s step_id=%s",
                               self.name, step_config["step_id"])
                break

        all_success = all(sr.success for sr in result.step_results) if result.step_results else True
        if result.status == FlowStatus.RUNNING and all_success:
            result.status = FlowStatus.COMPLETED
            result.success = True
            result.data = context.output_data

        return result

    def _execute_step_with_retry(self, step_config: dict, context: FlowContext) -> StepResult:
        """执行单个步骤，支持重试。

        Args:
            step_config: 步骤配置
            context: 流程上下文

        Returns:
            步骤执行结果
        """
        step_id = step_config["step_id"]
        step_func = step_config["step_func"]
        max_retries = step_config.get("retries", self.max_retries)
        step_result = None

        for attempt in range(max_retries + 1):  # 首次执行 + 重试次数
            # 步骤执行前钩子
            for hook in self._step_pre_hooks:
                try:
                    hook(step_id, context)
                except Exception as exc:  # noqa: BLE001
                    logger.error("flow_step_pre_hook_error step_id=%s error=%s", step_id, str(exc))

            start_time = time.time()  # 记录步骤开始时间
            try:
                # 调用步骤函数
                raw_result = step_func(context)
                latency_ms = (time.time() - start_time) * 1000  # 计算耗时

                # 如果返回的是 StepResult，直接使用
                if isinstance(raw_result, StepResult):
                    step_result = raw_result
                    step_result.latency_ms = latency_ms
                    if not step_result.step_id:
                        step_result.step_id = step_id
                    if not step_result.step_name:
                        step_result.step_name = step_id
                else:
                    # 否则包装为 StepResult
                    step_result = StepResult(
                        step_id=step_id,
                        step_name=step_id,
                        success=True,
                        data=raw_result,
                        latency_ms=latency_ms,
                    )

                # 将步骤结果存入共享状态
                context.shared_state[step_id] = step_result.data
                break  # 执行成功，跳出重试循环

            except Exception as exc:  # noqa: BLE001
                latency_ms = (time.time() - start_time) * 1000
                step_result = StepResult(
                    step_id=step_id,
                    step_name=step_id,
                    success=False,
                    error=str(exc),
                    latency_ms=latency_ms,
                )
                logger.warning("flow_step_failed flow=%s step_id=%s attempt=%d/%d error=%s",
                               self.name, step_id, attempt + 1, max_retries + 1, str(exc))

                if attempt < max_retries:
                    # 等待后重试
                    time.sleep(self.retry_delay_seconds)
                else:
                    break  # 重试耗尽

        return step_result or StepResult(
            step_id=step_id, step_name=step_id, success=False, error="unknown error"
        )

    async def _execute_step_with_retry_async(self, step_config: dict,
                                             context: FlowContext) -> StepResult:
        """异步执行单个步骤，支持重试。

        Args:
            step_config: 步骤配置
            context: 流程上下文

        Returns:
            步骤执行结果
        """
        step_id = step_config["step_id"]
        step_func = step_config["step_func"]
        max_retries = step_config.get("retries", self.max_retries)
        step_result = None

        for attempt in range(max_retries + 1):
            # 步骤执行前钩子
            for hook in self._step_pre_hooks:
                try:
                    res = hook(step_id, context)
                    if asyncio.iscoroutine(res):
                        await res
                except Exception as exc:  # noqa: BLE001
                    logger.error("flow_step_pre_hook_error step_id=%s error=%s", step_id, str(exc))

            start_time = time.time()
            try:
                # 判断是否为协程函数
                if asyncio.iscoroutinefunction(step_func):
                    raw_result = await step_func(context)  # 异步调用
                else:
                    raw_result = step_func(context)  # 同步调用（兼容）
                    if asyncio.iscoroutine(raw_result):
                        raw_result = await raw_result

                latency_ms = (time.time() - start_time) * 1000

                if isinstance(raw_result, StepResult):
                    step_result = raw_result
                    step_result.latency_ms = latency_ms
                    if not step_result.step_id:
                        step_result.step_id = step_id
                    if not step_result.step_name:
                        step_result.step_name = step_id
                else:
                    step_result = StepResult(
                        step_id=step_id,
                        step_name=step_id,
                        success=True,
                        data=raw_result,
                        latency_ms=latency_ms,
                    )

                context.shared_state[step_id] = step_result.data
                break

            except Exception as exc:  # noqa: BLE001
                latency_ms = (time.time() - start_time) * 1000
                step_result = StepResult(
                    step_id=step_id,
                    step_name=step_id,
                    success=False,
                    error=str(exc),
                    latency_ms=latency_ms,
                )
                logger.warning("flow_step_async_failed flow=%s step_id=%s attempt=%d/%d error=%s",
                               self.name, step_id, attempt + 1, max_retries + 1, str(exc))

                if attempt < max_retries:
                    await asyncio.sleep(self.retry_delay_seconds)
                else:
                    break  # 重试耗尽

        return step_result or StepResult(
            step_id=step_id, step_name=step_id, success=False, error="unknown error"
        )

    # ==================== 生命周期收尾与指标度量 ====================

    def _finalize(self, context: FlowContext, result: FlowResult) -> FlowResult:
        """收尾工作：记录结束时间、更新统计指标与链路日志。

        Args:
            context: 流程上下文
            result: 流程执行结果

        Returns:
            最终结果
        """
        context.end_time = time.time()
        result.total_latency_ms = context.get_elapsed_ms()

        with self._lock:
            self._execution_count += 1
            self._total_latency_ms += result.total_latency_ms

            if result.status == FlowStatus.COMPLETED:
                self._success_count += 1
                result.success = True
            elif result.status == FlowStatus.TIMEOUT:
                self._timeout_count += 1
                self._failure_count += 1
                result.success = False
            else:
                self._failure_count += 1
                result.success = False

        logger.info(
            "flow_finished flow=%s flow_id=%s status=%s latency=%.2fms success=%s steps=%d",
            self.name,
            context.flow_id,
            result.status.value,
            result.total_latency_ms,
            result.success,
            len(result.step_results),
        )
        return result

    def get_metrics(self) -> dict[str, Any]:
        """获取流程执行的历史统计度量指标。"""
        with self._lock:
            exec_cnt = self._execution_count
            avg_latency = (self._total_latency_ms / exec_cnt) if exec_cnt > 0 else 0.0
            success_rate = (self._success_count / exec_cnt) if exec_cnt > 0 else 0.0
            return {
                "flow_name": self.name,
                "execution_count": exec_cnt,
                "success_count": self._success_count,
                "failure_count": self._failure_count,
                "timeout_count": self._timeout_count,
                "total_latency_ms": round(self._total_latency_ms, 2),
                "avg_latency_ms": round(avg_latency, 2),
                "success_rate": round(success_rate, 4),
            }

    def reset_metrics(self) -> None:
        """重置统计度量指标。"""
        with self._lock:
            self._execution_count = 0
            self._success_count = 0
            self._failure_count = 0
            self._timeout_count = 0
            self._total_latency_ms = 0.0

    def reset(self) -> None:
        """重置流程状态与已注册步骤。"""
        with self._lock:
            self._steps.clear()
            self._pre_hooks.clear()
            self._post_hooks.clear()
            self._step_pre_hooks.clear()
            self._step_post_hooks.clear()
            self.build_steps()


__all__ = [
    "BaseFlow",
    "FlowContext",
    "FlowPriority",
    "FlowResult",
    "FlowStatus",
    "StepResult",
]
