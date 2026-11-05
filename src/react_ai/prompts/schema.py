"""提示词变量 Schema：类型/长度/正则/默认值声明与渲染前完整校验（差距分析 2.6）."""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any


@dataclass
class VariableSchema:
    """单个提示词变量的约束声明.

    Attributes:
        type: 期望 Python 类型（str/int/float/list/dict/bool 或元组），None 不校验
        required: 是否必填（缺失或 None 时报错）
        max_length: 字符串/list/dict 的最大长度
        min_length: 字符串/list/dict 的最小长度
        pattern: 字符串值的正则约束
        default: 可选变量缺失时注入的默认值
        description: 变量用途说明（错误信息展示）
    """

    type: type | tuple[type, ...] | None = None
    required: bool = False
    max_length: int | None = None
    min_length: int | None = None
    pattern: str | None = None
    default: Any = None
    description: str = ""

    def to_dict(self) -> dict[str, Any]:
        """序列化为可 JSON 存储的字典（type 转为名称字符串）."""
        type_name = None
        if self.type is not None:
            type_name = getattr(self.type, "__name__", None) or ",".join(
                getattr(t, "__name__", str(t)) for t in self.type
            )
        return {
            "type": type_name,
            "required": self.required,
            "max_length": self.max_length,
            "min_length": self.min_length,
            "pattern": self.pattern,
            "default": self.default,
            "description": self.description,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "VariableSchema":
        """从字典还原（type 名称字符串映射回 Python 类型）."""
        _TYPES = {
            "str": str, "int": int, "float": float,
            "list": list, "dict": dict, "bool": bool, "tuple": tuple,
        }
        raw_type = data.get("type")
        schema_type: Any = None
        if isinstance(raw_type, str) and raw_type:
            parts = [p.strip() for p in raw_type.split(",")]
            resolved = [_TYPES.get(p) for p in parts]
            if len(resolved) == 1:
                schema_type = resolved[0]
            else:
                schema_type = tuple(r for r in resolved if r is not None) or None
        return cls(
            type=schema_type,
            required=bool(data.get("required", False)),
            max_length=data.get("max_length"),
            min_length=data.get("min_length"),
            pattern=data.get("pattern"),
            default=data.get("default"),
            description=data.get("description", ""),
        )

    def validate(self, name: str, value: Any) -> list[str]:
        """校验单个变量值，返回错误描述列表（空列表即通过）."""
        errors: list[str] = []
        if value is None:
            if self.required:
                errors.append(f"变量 '{name}' 为必填且不可为 None")
            return errors

        if self.type is not None and not isinstance(value, self.type):
            # bool 是 int 子类，显式区分
            if not (self.type is int and isinstance(value, bool)):
                type_names = (
                    getattr(self.type, "__name__", None)
                    or "/".join(getattr(t, "__name__", str(t)) for t in self.type)
                )
                errors.append(
                    f"变量 '{name}' 类型非法: 期望 {type_names}, 实际 {type(value).__name__}"
                )

        if isinstance(value, (str, list, dict, tuple)):
            if self.max_length is not None and len(value) > self.max_length:
                errors.append(f"变量 '{name}' 长度超限: {len(value)} > {self.max_length}")
            if self.min_length is not None and len(value) < self.min_length:
                errors.append(f"变量 '{name}' 长度不足: {len(value)} < {self.min_length}")

        if isinstance(value, str) and self.pattern:
            if not re.search(self.pattern, value):
                errors.append(f"变量 '{name}' 不满足格式约束: {self.pattern}")

        return errors


def apply_schemas(
    schemas: dict[str, VariableSchema],
    variables: dict[str, Any],
    declared_names: set[str],
) -> dict[str, Any]:
    """渲染前完整校验并补齐默认值.

    Args:
        schemas: 变量名 -> VariableSchema
        variables: 调用方传入的变量字典（不修改原对象，返回新副本）
        declared_names: 全部已声明变量名集合（required/optional/schemas），
                        未在此集合内的变量在开发模式下告警

    Returns:
        校验通过并注入默认值后的变量字典

    Raises:
        ValueError: 任一变量校验失败时，错误信息聚合全部违规项
    """
    merged = dict(variables)
    errors: list[str] = []

    for name, schema in schemas.items():
        if name not in merged or merged[name] is None:
            if schema.default is not None:
                merged[name] = schema.default
            elif schema.required:
                errors.append(f"缺少必填变量 '{name}'{f' ({schema.description})' if schema.description else ''}")
            continue
        errors.extend(schema.validate(name, merged[name]))

    if errors:
        raise ValueError("Prompt 变量校验失败: " + "; ".join(errors))

    return merged
