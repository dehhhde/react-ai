"""统一工具基类、调用上下文、结果契约与声明式装饰器."""
import asyncio
import inspect
import logging  # 用于记录工具调用日志，便于问题排查和审计
import time  # 用于记录工具调用耗时，支持性能监控和审计
import uuid  # 用于生成唯一的 trace_id，方便链路追踪
from abc import ABC, abstractmethod  # ABC 是抽象基类，abstractmethod 用于强制子类实现指定方法
from dataclasses import dataclass, field  # dataclass 简化数据类定义，field 用于设置字段默认值工厂
from typing import Any, Callable, ClassVar, Optional

from pydantic import BaseModel, ConfigDict, ValidationError, create_model, field_validator

from react_ai.schemas.tools import ToolDefinition, ToolParameterSchema

logger = logging.getLogger(__name__)  # 获取当前模块的日志记录器，用于记录工具调用日志


class ToolInputModel(BaseModel):
    """工具输入参数的 Pydantic 基类.

    作为给 LLM 的 JSON Schema 与运行期参数校验的单一事实来源。
    - extra="forbid"：禁止额外字段，等价于 JSON Schema 的 additionalProperties: false
    - 数值字段拒绝 bool 传入（修复 bool 是 int 子类导致 flag: true 通过 integer 校验的缺陷）
    """

    model_config = ConfigDict(extra="forbid")

    @field_validator("*", mode="before")
    @classmethod
    def _reject_bool_for_numeric_fields(cls, v: Any, info):
        field_info = cls.model_fields.get(info.field_name)
        if field_info is None:
            return v
        annotation = field_info.annotation
        # 仅对原始 int/float 注解拒绝 bool（Optional/Union 等由 Pydantic 自身处理）
        if annotation in (int, float) and isinstance(v, bool):
            raise ValueError(f"字段 [{info.field_name}] 不接受布尔值，请传入数值")
        return v


@dataclass
class ToolContext:
    """调用上下文，不信任模型传入的身份信息。"""
    caller_id: str  # 调用方标识，例如 Agent 名称或服务名，用于审计谁调用了工具
    tenant_id: str  # 租户ID，用于多租户隔离，确保数据归属正确
    user_id: str  # 最终用户ID，用于权限校验和操作审计
    trace_id: str = field(default_factory=lambda: uuid.uuid4().hex)  # 链路追踪ID，默认自动生成，用于串联一次完整请求的所有日志
    idempotency_key: str | None = None  # 幂等键，写操作时必填，防止重试或断点恢复时重复执行


@dataclass
class ToolResult:
    ok: bool  # 调用是否成功，True 表示成功，False 表示失败
    data: Any = None  # 成功时返回的业务数据，失败时为 None
    error_code: str | None = None  # 错误码，用于程序化判断错误类型，如 invalid_arguments、permission_denied
    error_message: str | None = None  # 错误描述信息，用于人工排查或返回给模型做纠错
    error_type: str | None = None  # 错误大类：schema（参数错误）、business（业务拒绝）、auth（权限不足）、external（外部故障）
    retryable: bool = False  # 是否可重试，仅 external 类型错误可重试，其他类型重试无意义
    tool_name: str | None = None  # 工具名称，用于日志和审计，标识是哪个工具返回的结果
    trace_id: str | None = None  # 链路追踪ID，与 ToolContext 中的 trace_id 对应，方便关联
    replayed: bool = False  # 幂等重放标记：True 表示结果为同键首次成功执行的缓存重放


class ToolExecutionError(Exception):
    """工具执行失败异常：携带完整 ToolResult，供引擎回喂模型自我纠错.

    不再用裸 RuntimeError 丢失 error_type / retryable 等结构化信息。
    """

    def __init__(self, result: ToolResult):
        self.result = result
        super().__init__(f"[{result.error_code}] {result.error_message}")


class ToolFailureBudgetExceeded(Exception):
    """连续工具失败熔断：超过预算后终止整个 run，避免无效消耗与失控循环."""


class BaseTool(ABC):
    """工具基类：统一契约、校验、执行和审计。"""

    name: str  # 工具名称，全局唯一，用于日志、审计和模型选择工具
    description: str  # 工具描述，告诉模型这个工具能做什么、何时调用
    side_effects: str = "none"  # 副作用类型：none（无副作用）、read（只读）、write（写操作）、destructive（破坏性操作）
    tags: list[str] = field(default_factory=list)  # 工具标签，用于分类和检索，如 ["order", "query"]
    requires_approval: bool = False
    timeout_seconds: float = 30.0
    version: str = "1.0.0"  # 工具版本，用于灰度迁移与变更公告

    # Pydantic 输入/输出模型：作为给 LLM 的 JSON Schema 与运行期校验的单一事实来源。
    # 子类设置 input_model 后，input_schema 与 validate() 自动派生，无需手写。
    input_model: ClassVar[type[BaseModel] | None] = None
    output_model: ClassVar[type[BaseModel] | None] = None

    def __init__(self):
        if not isinstance(getattr(self, "tags", None), list):
            self.tags = []

    @property
    def input_schema(self) -> dict:
        """返回 JSON Schema 风格的输入定义，用于参数校验和模型提示.

        优先从 input_model 派生（单一事实来源）；子类也可直接覆盖本属性返回手写 dict。
        使用 by_alias=True 确保字段别名（如 json）在给 LLM 的 Schema 中正确呈现。
        """
        if self.input_model is not None:
            return self.input_model.model_json_schema(by_alias=True)
        return {}

    @property
    def output_schema(self) -> dict:
        """返回输出数据的 Schema 定义，默认返回通用对象类型，子类可覆盖。"""
        if self.output_model is not None:
            return self.output_model.model_json_schema()
        return {"type": "object"}

    def validate(self, arguments: dict) -> list[dict]:
        """参数校验.

        若设置了 input_model，使用 Pydantic 校验（支持嵌套/枚举/范围/格式/联合类型/类型强转）；
        否则回退到基于 input_schema dict 的手写校验（兼容旧工具）。
        """
        if self.input_model is not None:
            return self._validate_with_pydantic(arguments)
        return self._validate_with_schema_dict(arguments)

    def _validate_with_pydantic(self, arguments: dict) -> list[dict]:
        """使用 Pydantic 模型校验，将 ValidationError 转为统一的错误列表格式."""
        try:
            self.input_model.model_validate(arguments)
        except ValidationError as exc:
            errors: list[dict] = []
            for err in exc.errors():
                loc = err.get("loc", ())
                field_name = ".".join(str(p) for p in loc) if loc else "<root>"
                err_type = err.get("type", "")
                # 将 Pydantic 错误类型映射到现有 reason，便于错误 Observation 回喂模型
                reason = self._map_pydantic_error_type(err_type, err.get("ctx", {}))
                errors.append({
                    "field": field_name,
                    "reason": reason,
                    "message": err.get("msg", ""),
                    "input": err.get("input"),
                })
            return errors
        return []

    @staticmethod
    def _map_pydantic_error_type(err_type: str, ctx: dict) -> str:
        """将 Pydantic 错误类型映射为工具契约内的 reason 标识."""
        mapping = {
            "missing": "required_field_missing",
            "extra_forbidden": "unknown_field",
            "string_type": "type_mismatch",
            "int_type": "type_mismatch",
            "float_type": "type_mismatch",
            "bool_type": "type_mismatch",
            "list_type": "type_mismatch",
            "dict_type": "type_mismatch",
            "literal_error": "invalid_enum_value",
            "enum": "invalid_enum_value",
            "greater_than_equal": "below_minimum",
            "less_than_equal": "above_maximum",
            "greater_than": "below_minimum",
            "less_than": "above_maximum",
            "string_too_short": "below_min_length",
            "string_too_long": "above_max_length",
            "value_error": "invalid_value",
        }
        return mapping.get(err_type, err_type)

    def _validate_with_schema_dict(self, arguments: dict) -> list[dict]:
        """基于手写 input_schema dict 的兼容校验器（旧工具回退路径）."""
        schema = self.input_schema or {}
        errors: list[dict] = []

        props = schema.get("properties", {})
        required = set(schema.get("required", []))

        for field_name in required:
            if field_name not in arguments or arguments[field_name] is None:
                errors.append({"field": field_name, "reason": "required_field_missing"})

        if schema.get("additionalProperties") is False:
            for key in arguments:
                if key not in props:
                    errors.append({"field": key, "reason": "unknown_field"})

        for key, value in arguments.items():
            if key not in props:
                continue
            prop = props[key]
            expected_type = prop.get("type")

            if expected_type == "string" and not isinstance(value, str):
                errors.append({"field": key, "reason": "type_mismatch", "expected": "string"})
            elif expected_type == "integer" and not isinstance(value, int):
                errors.append({"field": key, "reason": "type_mismatch", "expected": "integer"})
            elif expected_type == "number" and not isinstance(value, (int, float)):
                errors.append({"field": key, "reason": "type_mismatch", "expected": "number"})
            elif expected_type == "boolean" and not isinstance(value, bool):
                errors.append({"field": key, "reason": "type_mismatch", "expected": "boolean"})

            if "enum" in prop and value not in prop["enum"]:
                errors.append({"field": key, "reason": "invalid_enum_value", "expected": prop["enum"], "got": value})

            if isinstance(value, (int, float)):
                if "minimum" in prop and value < prop["minimum"]:
                    errors.append({"field": key, "reason": "below_minimum", "minimum": prop["minimum"]})
                if "maximum" in prop and value > prop["maximum"]:
                    errors.append({"field": key, "reason": "above_maximum", "maximum": prop["maximum"]})

        return errors

    def _validate_output(self, data: Any) -> list[dict]:
        """输出侧校验：若设置了 output_model，对 execute 返回值做 Pydantic 校验."""
        if self.output_model is None or data is None:
            return []
        try:
            if isinstance(data, dict):
                self.output_model.model_validate(data)
            else:
                self.output_model.model_validate(data)
        except ValidationError as exc:
            return [
                {
                    "field": ".".join(str(p) for p in err.get("loc", ())) or "<root>",
                    "reason": "output_schema_violation",
                    "message": err.get("msg", ""),
                }
                for err in exc.errors()
            ]
        return []

    def _fail(self, error_type: str, error_code: str, message: str, trace_id: str) -> ToolResult:
        """构造一个失败的 ToolResult，统一错误格式。"""
        return ToolResult(
            ok=False,  # 标记为失败
            error_type=error_type,  # 错误大类，决定模型能否自我修复
            error_code=error_code,  # 具体错误码
            error_message=message,  # 错误描述
            retryable=error_type == "external",  # 仅外部故障可重试，schema/business/auth 错误重试无意义
            trace_id=trace_id,  # 附带链路追踪ID
            tool_name=self.name,  # 附带工具名称
        )

    def _idempotency_begin(self, ctx: ToolContext) -> "IdempotencyDecision | None":
        """写/破坏性操作执行前的幂等占位判定；非写操作返回 None.

        - replay: 同键已有成功结果，直接重放缓存
        - conflict: 同键调用正在执行中，返回可重试的外部错误
        - proceed: 占位成功，允许执行（首次调用或失败后重试）
        """
        from react_ai.idempotency.guard import get_idempotency_guard
        if self.side_effects not in {"write", "destructive"} or not ctx.idempotency_key:
            return None
        return get_idempotency_guard().begin(self.name, ctx.tenant_id, ctx.idempotency_key)

    def call(self, arguments: dict, ctx: ToolContext) -> ToolResult:
        """工具调用的统一入口：校验 → 幂等闸门 → 执行 → 异常捕获 → 审计日志。"""
        from react_ai.idempotency.guard import get_idempotency_guard
        trace_id = ctx.trace_id  # 从上下文中提取 trace_id，用于全链路追踪
        start = time.time()  # 记录调用开始时间，用于计算耗时
        result: ToolResult | None = None
        decision = None  # 幂等占位决策（None 表示未持有占位，无需回写状态）

        try:
            errors = self.validate(arguments)  # 执行参数校验
            if errors:
                result = self._fail("schema", "invalid_arguments", str(errors), trace_id)
                return result  # 校验失败直接返回，不执行

            # 写操作必须携带幂等键，防止重试或断点恢复时重复执行
            if self.side_effects in {"write", "destructive"} and not ctx.idempotency_key:
                result = self._fail(
                    "schema",
                    "missing_idempotency_key",  # 错误码：缺少幂等键
                    "写操作必须提供幂等键",  # 错误描述
                    trace_id,
                )
                return result

            decision = self._idempotency_begin(ctx)
            if decision is not None:
                if decision.action == "replay":
                    cached: ToolResult = decision.result
                    result = ToolResult(
                        ok=cached.ok, data=cached.data,
                        error_code=cached.error_code, error_message=cached.error_message,
                        error_type=cached.error_type, retryable=False,
                        tool_name=self.name, trace_id=trace_id, replayed=True,
                    )
                    return result
                if decision.action == "conflict":
                    result = self._fail(
                        "external", "idempotency_conflict",
                        "相同幂等键的调用正在执行中，请稍后重试", trace_id,
                    )
                    return result

            exec_data = self.execute(arguments, ctx)  # 调用子类实现的真实业务逻辑

            # 输出侧校验：若设置了 output_model，对返回值做 Schema 校验，避免脏数据进上下文
            out_errors = self._validate_output(exec_data)
            if out_errors:
                result = self._fail("schema", "output_schema_violation", str(out_errors), trace_id)
                if decision is not None:
                    get_idempotency_guard().failure(decision)
                return result

            result = ToolResult(
                ok=True,  # 标记为成功
                data=exec_data,  # 业务返回数据
                trace_id=trace_id,  # 附带链路追踪ID
                tool_name=self.name,  # 附带工具名称
            )
            if decision is not None:
                get_idempotency_guard().success(decision, result)
            return result

        except PermissionError as exc:
            # 权限异常：用户没有执行该操作的权限
            result = self._fail("auth", "permission_denied", str(exc), trace_id)
            if decision is not None:
                get_idempotency_guard().failure(decision)
            return result
        except ValueError as exc:
            # 业务异常：参数合法但业务规则拒绝，如余额不足、订单已关闭
            result = self._fail("business", "business_rejected", str(exc), trace_id)
            if decision is not None:
                get_idempotency_guard().failure(decision)
            return result
        except Exception as exc:
            # 未预期异常：记录完整堆栈，返回 external 错误，允许上层重试
            logger.exception("tool_call_failed tool=%s trace=%s", self.name, trace_id)
            result = self._fail("external", "tool_execution_failed", str(exc), trace_id)
            if decision is not None:
                get_idempotency_guard().failure(decision)
            return result

        finally:
            duration = time.time() - start  # 计算调用耗时
            ok = result.ok if result is not None else False
            # 可观测性接线：记录工具调用指标（成功率/错误率/耗时分布）
            # 指标/审计统一在 ToolSandbox.run_safe 出口记录，此处仅保留日志，避免双计
            logger.info(
                "tool_called tool=%s ok=%s duration_ms=%d trace=%s",
                self.name,  # 工具名称
                ok,  # 是否成功
                int(duration * 1000),  # 耗时转换为毫秒
                trace_id,  # 链路追踪ID
            )

    @abstractmethod
    def execute(self, arguments: dict, ctx: ToolContext) -> Any:
        """子类实现真实业务逻辑，基类保证在此之前参数已校验通过。"""

    async def acall(self, arguments: dict, ctx: ToolContext) -> ToolResult:
        """异步统一调用入口：支持异步与同步 execute，统一完成校验、幂等闸门与异常审计。"""
        from react_ai.idempotency.guard import get_idempotency_guard
        trace_id = ctx.trace_id
        start = time.time()
        result: ToolResult | None = None
        decision = None  # 幂等占位决策（None 表示未持有占位，无需回写状态）

        try:
            errors = self.validate(arguments)
            if errors:
                result = self._fail("schema", "invalid_arguments", str(errors), trace_id)
                return result

            if self.side_effects in {"write", "destructive"} and not ctx.idempotency_key:
                result = self._fail(
                    "schema",
                    "missing_idempotency_key",
                    "写操作必须提供幂等键",
                    trace_id,
                )
                return result

            decision = self._idempotency_begin(ctx)
            if decision is not None:
                if decision.action == "replay":
                    cached: ToolResult = decision.result
                    result = ToolResult(
                        ok=cached.ok, data=cached.data,
                        error_code=cached.error_code, error_message=cached.error_message,
                        error_type=cached.error_type, retryable=False,
                        tool_name=self.name, trace_id=trace_id, replayed=True,
                    )
                    return result
                if decision.action == "conflict":
                    result = self._fail(
                        "external", "idempotency_conflict",
                        "相同幂等键的调用正在执行中，请稍后重试", trace_id,
                    )
                    return result

            # 同步 execute 卸载到线程池，避免阻塞 FastAPI 事件循环；异步 execute 直接 await
            if inspect.iscoroutinefunction(self.execute):
                exec_data = await self.execute(arguments, ctx)
            else:
                exec_data = await asyncio.to_thread(self.execute, arguments, ctx)
            if inspect.isawaitable(exec_data):
                exec_data = await exec_data

            # execute 可能直接返回 ToolResult（含失败结果），此时透传保留 error_type/retryable
            if isinstance(exec_data, ToolResult):
                result = exec_data
            else:
                # 输出侧校验：若设置了 output_model，对返回值做 Schema 校验，避免脏数据进上下文
                out_errors = self._validate_output(exec_data)
                if out_errors:
                    result = self._fail("schema", "output_schema_violation", str(out_errors), trace_id)
                    if decision is not None:
                        get_idempotency_guard().failure(decision)
                    return result
                result = ToolResult(
                    ok=True,
                    data=exec_data,
                    trace_id=trace_id,
                    tool_name=self.name,
                )
            if decision is not None:
                if result.ok:
                    get_idempotency_guard().success(decision, result)
                else:
                    get_idempotency_guard().failure(decision)
            return result

        except PermissionError as exc:
            result = self._fail("auth", "permission_denied", str(exc), trace_id)
            if decision is not None:
                get_idempotency_guard().failure(decision)
            return result
        except ValueError as exc:
            result = self._fail("business", "business_rejected", str(exc), trace_id)
            if decision is not None:
                get_idempotency_guard().failure(decision)
            return result
        except Exception as exc:
            logger.exception("tool_call_failed tool=%s trace=%s", self.name, trace_id)
            result = self._fail("external", "tool_execution_failed", str(exc), trace_id)
            if decision is not None:
                get_idempotency_guard().failure(decision)
            return result

        finally:
            duration = time.time() - start
            ok = result.ok if result is not None else False
            # 指标/审计统一在 ToolSandbox.run_safe 出口记录，此处仅保留日志，避免双计
            logger.info(
                "tool_called tool=%s ok=%s duration_ms=%d trace=%s",
                self.name,
                ok,
                int(duration * 1000),
                trace_id,
            )

    def get_definition(self) -> ToolDefinition:
        """获取工具元数据定义，用于注册表与大模型 Function Calling。"""
        schema = self.input_schema or {}
        return ToolDefinition(
            name=self.name,
            description=self.description,
            parameters=ToolParameterSchema(
                type=schema.get("type", "object"),
                properties=schema.get("properties", {}),
                required=schema.get("required", []),
            ),
            requires_approval=getattr(self, "requires_approval", False) or (self.side_effects in {"write", "destructive"}),
            timeout_seconds=getattr(self, "timeout_seconds", 30.0),
            metadata={
                "side_effects": self.side_effects,
                "tags": self.tags if isinstance(getattr(self, "tags", None), list) else [],
                "output_schema": self.output_schema,
                "version": getattr(self, "version", "1.0.0"),
            },
        )


class FunctionTool(BaseTool):
    """基于普通函数或装饰器的函数工具适配器.

    输入 Schema 优先使用显式传入的 input_model；否则用 pydantic.create_model 从函数签名
    动态生成（自动识别 Optional[X]、list[str]、默认值、嵌套注解等）。
    """

    def __init__(
        self,
        func: Callable,
        name: Optional[str] = None,
        description: Optional[str] = None,
        side_effects: str = "none",
        tags: Optional[list[str]] = None,
        requires_approval: bool = False,
        timeout_seconds: float = 30.0,
        input_schema: Optional[dict] = None,
        input_model: Optional[type[BaseModel]] = None,
    ):
        super().__init__()
        self.func = func
        self.name = name or func.__name__
        self.description = description or func.__doc__ or "自定义函数工具"
        self.side_effects = side_effects
        self.tags = tags or []
        self.requires_approval = requires_approval
        self.timeout_seconds = timeout_seconds
        self._custom_input_schema = input_schema

        if input_model is not None:
            # 显式传入的 Pydantic 模型作为单一事实来源
            self.input_model = input_model
        elif input_schema is not None:
            # 兼容：传入了手写 dict schema 时仍走旧校验路径
            self.input_model = None
        else:
            # 从函数签名动态生成 Pydantic 模型（支持 Optional/list/默认值等）
            self.input_model = self._build_model_from_func()

    @property
    def input_schema(self) -> dict:
        if self.input_model is not None:
            return self.input_model.model_json_schema()
        if self._custom_input_schema:
            return self._custom_input_schema
        return {}

    def _build_model_from_func(self) -> type[BaseModel]:
        """从函数签名动态构造 Pydantic 模型，作为输入校验与 Schema 生成的单一事实来源."""
        sig = inspect.signature(self.func)
        field_defs: dict[str, Any] = {}

        for param_name, param in sig.parameters.items():
            if param_name in ("ctx", "context", "run_context", "tool_context"):
                continue

            annotation = param.annotation if param.annotation is not inspect.Parameter.empty else Any
            if param.default is inspect.Parameter.empty:
                # 必填字段：(type, ...) 中 ... 表示无默认值
                field_defs[param_name] = (annotation, ...)
            else:
                field_defs[param_name] = (annotation, param.default)

        model_name = f"{self.name.title().replace('_', '')}Input"
        return create_model(
            model_name,
            __base__=ToolInputModel,
            **field_defs,
        )

    def execute(self, arguments: dict, ctx: ToolContext) -> Any:
        sig = inspect.signature(self.func)
        kwargs = {}
        for param_name, param in sig.parameters.items():
            if param_name in ("ctx", "tool_context", "context"):
                kwargs[param_name] = ctx
            elif param_name in arguments:
                kwargs[param_name] = arguments[param_name]

        return self.func(**kwargs)


def tool(
    name: Optional[str] = None,
    description: Optional[str] = None,
    side_effects: str = "none",
    tags: Optional[list[str]] = None,
    requires_approval: bool = False,
    timeout_seconds: float = 30.0,
    input_schema: Optional[dict] = None,
    input_model: Optional[type[BaseModel]] = None,
):
    """声明式工具装饰器，将普通 Python 函数包装为 BaseTool 实例.

    可通过 input_model 传入 Pydantic 模型作为输入契约（推荐）；
    未提供时自动从函数签名推导。
    """
    def decorator(func: Callable) -> FunctionTool:
        return FunctionTool(
            func=func,
            name=name,
            description=description,
            side_effects=side_effects,
            tags=tags,
            requires_approval=requires_approval,
            timeout_seconds=timeout_seconds,
            input_schema=input_schema,
            input_model=input_model,
        )
    return decorator

