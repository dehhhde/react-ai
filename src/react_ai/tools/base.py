"""统一工具基类、调用上下文、结果契约与声明式装饰器."""
import inspect
from react_ai.schemas.tools import ToolDefinition, ToolParameterSchema
from typing import Callable, Optional
import time  # 用于记录工具调用耗时，支持性能监控和审计
import uuid  # 用于生成唯一的 trace_id，方便链路追踪
import logging  # 用于记录工具调用日志，便于问题排查和审计
from abc import ABC, abstractmethod  # ABC 是抽象基类，abstractmethod 用于强制子类实现指定方法
from dataclasses import dataclass, field  # dataclass 简化数据类定义，field 用于设置字段默认值工厂
from typing import Any  # Any 表示任意类型，用于工具返回值的泛型声明

logger = logging.getLogger(__name__)  # 获取当前模块的日志记录器，用于记录工具调用日志


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


class BaseTool(ABC):
    """工具基类：统一契约、校验、执行和审计。"""

    name: str  # 工具名称，全局唯一，用于日志、审计和模型选择工具
    description: str  # 工具描述，告诉模型这个工具能做什么、何时调用
    side_effects: str = "none"  # 副作用类型：none（无副作用）、read（只读）、write（写操作）、destructive（破坏性操作）
    tags: list[str] = field(default_factory=list)  # 工具标签，用于分类和检索，如 ["order", "query"]
    requires_approval: bool = False
    timeout_seconds: float = 30.0

    def __init__(self):
        if not isinstance(getattr(self, "tags", None), list):
            self.tags = []

    @property
    @abstractmethod
    def input_schema(self) -> dict:
        """返回 JSON Schema 风格的输入定义，用于参数校验和模型提示。"""

    @property
    def output_schema(self) -> dict:
        """返回输出数据的 Schema 定义，默认返回通用对象类型，子类可覆盖。"""
        return {"type": "object"}

    def validate(self, arguments: dict) -> list[dict]:
        """默认做基础校验：必填、类型、枚举、范围、额外字段。"""
        schema = self.input_schema  # 获取当前工具的输入 Schema 定义
        errors: list[dict] = []  # 初始化错误列表，用于收集所有校验失败信息

        props = schema.get("properties", {})  # 获取 Schema 中定义的合法字段及其约束
        required = set(schema.get("required", []))  # 获取必填字段集合，转为 set 提高查找效率

        # 必填字段校验：遍历所有必填字段，检查是否缺失或为 None
        for field_name in required:
            if field_name not in arguments or arguments[field_name] is None:
                errors.append({
                    "field": field_name,  # 出错的字段名
                    "reason": "required_field_missing",  # 错误原因：必填字段缺失
                })

        # 未知字段校验：如果 Schema 禁止额外字段，检查是否存在未定义的字段
        if schema.get("additionalProperties") is False:
            for key in arguments:
                if key not in props:
                    errors.append({
                        "field": key,  # 未定义的字段名
                        "reason": "unknown_field",  # 错误原因：未知字段
                    })

        # 类型、枚举、范围校验：遍历所有传入的参数，逐个检查
        for key, value in arguments.items():
            if key not in props:  # 跳过未知字段，未知字段已在上面单独处理
                continue

            prop = props[key]  # 获取当前字段的 Schema 定义
            expected_type = prop.get("type")  # 获取期望的数据类型

            # 字符串类型校验
            if expected_type == "string" and not isinstance(value, str):
                errors.append({
                    "field": key,
                    "reason": "type_mismatch",  # 错误原因：类型不匹配
                    "expected": "string",  # 期望类型
                })
            # 整数类型校验，注意 bool 是 int 的子类，但这里不额外排除，由业务决定是否需要
            elif expected_type == "integer" and not isinstance(value, int):
                errors.append({
                    "field": key,
                    "reason": "type_mismatch",
                    "expected": "integer",
                })
            # 数字类型校验，允许 int 或 float
            elif expected_type == "number" and not isinstance(value, (int, float)):
                errors.append({
                    "field": key,
                    "reason": "type_mismatch",
                    "expected": "number",
                })
            # 布尔类型校验
            elif expected_type == "boolean" and not isinstance(value, bool):
                errors.append({
                    "field": key,
                    "reason": "type_mismatch",
                    "expected": "boolean",
                })

            # 枚举值校验：如果字段定义了 enum，检查值是否在允许范围内
            if "enum" in prop and value not in prop["enum"]:
                errors.append({
                    "field": key,
                    "reason": "invalid_enum_value",  # 错误原因：枚举值非法
                    "expected": prop["enum"],  # 允许的枚举值列表
                    "got": value,  # 实际传入的值
                })

            # 数值范围校验：仅对 int 或 float 类型检查 minimum 和 maximum
            if isinstance(value, (int, float)):
                if "minimum" in prop and value < prop["minimum"]:
                    errors.append({
                        "field": key,
                        "reason": "below_minimum",  # 错误原因：低于最小值
                        "minimum": prop["minimum"],  # 允许的最小值
                    })
                if "maximum" in prop and value > prop["maximum"]:
                    errors.append({
                        "field": key,
                        "reason": "above_maximum",  # 错误原因：超过最大值
                        "maximum": prop["maximum"],  # 允许的最大值
                    })

        return errors  # 返回所有校验错误，空列表表示校验通过

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

    def call(self, arguments: dict, ctx: ToolContext) -> ToolResult:
        """工具调用的统一入口：校验 → 幂等检查 → 执行 → 异常捕获 → 审计日志。"""
        trace_id = ctx.trace_id  # 从上下文中提取 trace_id，用于全链路追踪
        start = time.time()  # 记录调用开始时间，用于计算耗时

        try:
            errors = self.validate(arguments)  # 执行参数校验
            if errors:
                return self._fail("schema", "invalid_arguments", str(errors), trace_id)  # 校验失败直接返回，不执行

            # 写操作必须携带幂等键，防止重试或断点恢复时重复执行
            if self.side_effects in {"write", "destructive"} and not ctx.idempotency_key:
                return self._fail(
                    "schema",
                    "missing_idempotency_key",  # 错误码：缺少幂等键
                    "写操作必须提供幂等键",  # 错误描述
                    trace_id,
                )

            exec_data = self.execute(arguments, ctx)  # 调用子类实现的真实业务逻辑

            result = ToolResult(
                ok=True,  # 标记为成功
                data=exec_data,  # 业务返回数据
                trace_id=trace_id,  # 附带链路追踪ID
                tool_name=self.name,  # 附带工具名称
            )
            return result

        except PermissionError as exc:
            # 权限异常：用户没有执行该操作的权限
            result = self._fail("auth", "permission_denied", str(exc), trace_id)
            return result
        except ValueError as exc:
            # 业务异常：参数合法但业务规则拒绝，如余额不足、订单已关闭
            result = self._fail("business", "business_rejected", str(exc), trace_id)
            return result
        except Exception as exc:
            # 未预期异常：记录完整堆栈，返回 external 错误，允许上层重试
            logger.exception("tool_call_failed tool=%s trace=%s", self.name, trace_id)
            result = self._fail("external", "tool_execution_failed", str(exc), trace_id)
            return result

        finally:
            duration = time.time() - start  # 计算调用耗时
            logger.info(
                "tool_called tool=%s ok=%s duration_ms=%d trace=%s",
                self.name,  # 工具名称
                result.ok if ("result" in locals() and result is not None) else False,  # 是否成功，异常时 result 未定义，默认为 False
                int(duration * 1000),  # 耗时转换为毫秒
                trace_id,  # 链路追踪ID
            )

    @abstractmethod
    def execute(self, arguments: dict, ctx: ToolContext) -> Any:
        """子类实现真实业务逻辑，基类保证在此之前参数已校验通过。"""

    async def acall(self, arguments: dict, ctx: ToolContext) -> ToolResult:
        """异步统一调用入口：支持异步与同步 execute，统一完成校验、幂等检查与异常审计。"""
        trace_id = ctx.trace_id
        start = time.time()
        result: ToolResult | None = None

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

            exec_data = self.execute(arguments, ctx)
            if inspect.isawaitable(exec_data):
                exec_data = await exec_data

            result = ToolResult(
                ok=True,
                data=exec_data,
                trace_id=trace_id,
                tool_name=self.name,
            )
            return result

        except PermissionError as exc:
            result = self._fail("auth", "permission_denied", str(exc), trace_id)
            return result
        except ValueError as exc:
            result = self._fail("business", "business_rejected", str(exc), trace_id)
            return result
        except Exception as exc:
            logger.exception("tool_call_failed tool=%s trace=%s", self.name, trace_id)
            result = self._fail("external", "tool_execution_failed", str(exc), trace_id)
            return result

        finally:
            duration = time.time() - start
            logger.info(
                "tool_called tool=%s ok=%s duration_ms=%d trace=%s",
                self.name,
                result.ok if result is not None else False,
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
            },
        )


class FunctionTool(BaseTool):
    """基于普通函数或装饰器的函数工具适配器。"""

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

    @property
    def input_schema(self) -> dict:
        if self._custom_input_schema:
            return self._custom_input_schema
        return self._generate_schema_from_func()

    def _generate_schema_from_func(self) -> dict:
        sig = inspect.signature(self.func)
        properties = {}
        required = []

        type_map = {
            str: "string",
            int: "integer",
            float: "number",
            bool: "boolean",
            list: "array",
            dict: "object",
        }

        for param_name, param in sig.parameters.items():
            if param_name in ("ctx", "context", "run_context", "tool_context"):
                continue

            param_type = "string"
            if param.annotation != inspect.Parameter.empty:
                param_type = type_map.get(param.annotation, "string")

            properties[param_name] = {"type": param_type}
            if param.default == inspect.Parameter.empty:
                required.append(param_name)

        return {
            "type": "object",
            "properties": properties,
            "required": required,
        }

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
):
    """声明式工具装饰器，将普通 Python 函数包装为 BaseTool 实例。"""
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
        )
    return decorator

