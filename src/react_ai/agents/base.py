"""统一智能体基类、运行状态、上下文与审计契约."""
import logging
import time
import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

logger = logging.getLogger(__name__)


class AgentStatus(Enum):
    """Agent 运行状态枚举。"""
    IDLE = "idle"              # 空闲，未开始执行
    RUNNING = "running"        # 正在执行中
    COMPLETED = "completed"    # 正常完成
    FAILED = "failed"          # 执行失败
    WAITING = "waiting"        # 等待外部事件或人工审批


@dataclass
class AgentContext:
    """Agent 执行上下文，承载身份、追踪和运行时信息。"""
    trace_id: str = field(default_factory=lambda: uuid.uuid4().hex)  # 链路追踪ID，串联一次完整请求的所有日志
    session_id: str | None = None  # 会话ID，用于区分不同用户或对话窗口
    user_id: str | None = None  # 最终用户ID，用于权限校验和审计
    tenant_id: str | None = None  # 租户ID，用于多租户数据隔离
    max_steps: int = 20  # 最大执行步数，防止 Agent 无限循环或卡死
    timeout_seconds: float = 300.0  # 整体超时时间（秒），超时则强制终止
    metadata: dict[str, Any] = field(default_factory=dict)  # 扩展元数据，可携带业务标签、渠道信息等


@dataclass
class AgentStep:
    """单步执行记录，用于审计和回放。"""
    step_number: int  # 步骤序号，从 1 开始
    action: str  # 本步执行的动作，如 "think"、"call_tool"、"respond"
    input_data: dict[str, Any]  # 本步输入，如思考内容、工具参数
    output_data: dict[str, Any]  # 本步输出，如模型回复、工具结果
    duration_ms: int  # 本步耗时（毫秒）
    error: str | None = None  # 本步错误信息，无错误则为 None


@dataclass
class AgentResult:
    """Agent 最终执行结果。"""
    ok: bool  # 是否成功完成
    output: Any = None  # 最终输出内容，如回复文本、结构化数据
    status: AgentStatus = AgentStatus.IDLE  # 最终状态
    steps: list[AgentStep] = field(default_factory=list)  # 完整步骤记录，用于审计和调试
    trace_id: str | None = None  # 链路追踪ID
    error_message: str | None = None  # 失败时的错误描述
    total_duration_ms: int = 0  # 总耗时（毫秒）


class BaseAgent(ABC):
    """Agent 基类：统一身份、运行循环、步骤管理、异常处理和审计。"""

    name: str = ""  # Agent 唯一标识，如 "after_sales_router"
    description: str = ""  # Agent 职责描述，用于编排层识别和路由
    version: str = "1.0.0"  # 版本号，便于灰度和回滚
    tags: list[str] = field(default_factory=list)  # 标签，如 ["after_sales", "router"]

    def __init__(self):
        if not isinstance(getattr(self, "tags", None), list):
            self.tags = []
        self._status: AgentStatus = AgentStatus.IDLE  # 当前运行状态
        self._steps: list[AgentStep] = []  # 步骤历史记录
        self._context: AgentContext | None = None  # 当前执行上下文

    @property
    def status(self) -> AgentStatus:
        """获取当前 Agent 状态。"""
        return self._status

    @property
    def steps(self) -> list[AgentStep]:
        """获取已执行的步骤记录。"""
        return self._steps.copy()  # 返回副本，防止外部修改

    @property
    def context(self) -> AgentContext | None:
        """获取当前执行上下文。"""
        return self._context

    def _add_step(self, step: AgentStep):
        """记录一个执行步骤。"""
        self._steps.append(step)  # 追加到步骤列表
        logger.info(
            "agent_step name=%s step=%d action=%s duration_ms=%d trace=%s",
            getattr(self, "name", self.__class__.__name__),
            step.step_number,
            step.action,
            step.duration_ms,
            self._context.trace_id if self._context else None,
        )

    def _check_timeout(self, start_time: float, timeout: float) -> bool:
        """检查是否超时。"""
        return (time.time() - start_time) > timeout  # 当前时间减去开始时间超过阈值则超时

    def _build_result(self, ok: bool, output: Any, error_message: str | None, start_time: float) -> AgentResult:
        """构造最终结果。"""
        return AgentResult(
            ok=ok,  # 是否成功
            output=output,  # 最终输出
            status=self._status,  # 最终状态
            steps=self._steps.copy(),  # 步骤快照
            trace_id=self._context.trace_id if self._context else None,  # 链路ID
            error_message=error_message,  # 错误信息
            total_duration_ms=int((time.time() - start_time) * 1000),  # 总耗时
        )

    def run(self, input_data: dict[str, Any], ctx: AgentContext | None = None) -> AgentResult:
        """Agent 统一入口：初始化上下文 → 执行循环 → 返回结果。"""
        self._context = ctx or AgentContext()  # 使用传入上下文或创建默认上下文
        self._status = AgentStatus.RUNNING  # 标记为运行中
        self._steps.clear()  # 清空历史步骤，防止多次调用时数据污染
        start_time = time.time()  # 记录开始时间

        logger.info(
            "agent_started name=%s trace=%s session=%s user=%s",
            getattr(self, "name", self.__class__.__name__),
            self._context.trace_id,
            self._context.session_id,
            self._context.user_id,
        )

        try:
            # 执行前置校验：子类可覆盖以做额外检查
            self._pre_run_check(input_data, self._context)

            output = None  # 初始化输出变量
            for step_num in range(1, self._context.max_steps + 1):
                # 检查超时
                if self._check_timeout(start_time, self._context.timeout_seconds):
                    self._status = AgentStatus.FAILED
                    return self._build_result(
                        ok=False,
                        output=None,
                        error_message="Agent 执行超时",
                        start_time=start_time,
                    )

                # 执行单步逻辑
                step_result = self._step(step_num, input_data, self._context)
                self._add_step(step_result)

                # 判断是否需要继续
                if step_result.action == "respond":
                    output = step_result.output_data.get("content")  # 提取回复内容
                    self._status = AgentStatus.COMPLETED  # 标记完成
                    break

                # 如果子类返回了需要等待的状态，暂停执行
                if step_result.action == "wait":
                    self._status = AgentStatus.WAITING
                    output = step_result.output_data.get("content")
                    break

            # 如果跑满所有步数仍未 respond，视为卡死
            if self._status == AgentStatus.RUNNING:
                self._status = AgentStatus.FAILED
                return self._build_result(
                    ok=False,
                    output=None,
                    error_message=f"Agent 达到最大步数 {self._context.max_steps}，未能完成",
                    start_time=start_time,
                )

            return self._build_result(ok=True, output=output, error_message=None, start_time=start_time)

        except Exception as exc:
            self._status = AgentStatus.FAILED
            logger.exception(
                "agent_failed name=%s trace=%s",
                getattr(self, "name", self.__class__.__name__),
                self._context.trace_id if self._context else None,
            )
            return self._build_result(
                ok=False,
                output=None,
                error_message=str(exc),
                start_time=start_time,
            )

        finally:
            logger.info(
                "agent_finished name=%s status=%s steps=%d duration_ms=%d trace=%s",
                getattr(self, "name", self.__class__.__name__),
                self._status.value,
                len(self._steps),
                int((time.time() - start_time) * 1000),
                self._context.trace_id if self._context else None,
            )

    def _pre_run_check(self, input_data: dict[str, Any], ctx: AgentContext):
        """前置校验钩子，子类可覆盖以做参数校验、权限检查等。"""
        if not input_data:
            raise ValueError("input_data 不能为空")

    @abstractmethod
    def _step(self, step_number: int, input_data: dict[str, Any], ctx: AgentContext) -> AgentStep:
        """单步执行逻辑，子类必须实现。

        返回值中的 action 决定循环行为：
        - "think"：内部思考，继续下一步
        - "call_tool"：调用工具，继续下一步
        - "respond"：输出最终回复，结束循环
        - "wait"：等待外部事件或人工审批，暂停执行
        """

    def reset(self):
        """重置 Agent 状态，使其可以重新执行。"""
        self._status = AgentStatus.IDLE  # 恢复空闲状态
        self._steps.clear()  # 清空步骤记录
        self._context = None  # 清除上下文引用
