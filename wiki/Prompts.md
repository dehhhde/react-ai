# Prompts 提示词工程管理

模块：`react_ai.prompts`。集中管理提示词，消除散落在业务代码中的硬编码字符串，支持 Jinja2 动态插值与结构化 `BasePrompt` 实体两种模式。

## PromptManager

文件：[prompts/manager.py](file:///d:/code/react-ai/src/react_ai/prompts/manager.py)

```python
class PromptManager:
    def register(self, name: str, template_str: str) -> None        # 注册原始模板字符串
    def register_prompt(self, prompt: BasePrompt) -> None           # 注册结构化 Prompt 实体
    def get_prompt(self, name: str) -> BasePrompt | None
    def render(self, name: str, variables: dict) -> str             # Jinja2 渲染模板
    def render_prompt(self, name: str, variables: dict,
                      trace_id: str | None = None) -> PromptResult # 渲染 BasePrompt 实体（带审计）
```

内置注册项：

| 名称 | 类型 | 内容 |
|---|---|---|
| `react_system` | 模板字符串 | `REACT_SYSTEM_PROMPT` 企业标准 ReAct 系统提示词 |
| `react_standard` | `ReActPrompt` 实体 | 结构化渲染，输出 `PromptResult` |

## 用法

```python
from react_ai.prompts.manager import PromptManager

pm = PromptManager()

# 1) 模板模式
pm.register("support_greeting", "你是{{ company }}的客服助手，语言风格：{{ style }}。")
text = pm.render("support_greeting", {"company": "ACME", "style": "专业简洁"})

# 2) 实体模式（版本化、渲染审计）
result = pm.render_prompt("react_standard", {"tools": registry.list_definitions()},
                          trace_id=ctx.run_id)
system_prompt = result.content
```

## 预置 ReAct 模板

文件：[prompts/templates/react.py](file:///d:/code/react-ai/src/react_ai/prompts/templates/react.py)。定义经过验证的企业级 ReAct 系统提示词（约束 Thought/Action/Observation 输出格式与工具调用规范），`ReActPrompt` 为其 `BasePrompt` 封装。

运行时覆盖：`AgentConfig.system_prompt_override` 可直接替换 Agent 使用的系统提示词（见 [Core Engine](Core-Engine.md)）。

## BasePrompt 契约

文件：[prompts/base.py](file:///d:/code/react-ai/src/react_ai/prompts/base.py)。定义 `PromptMessage` 消息协议、`PromptResult` 渲染契约与 `render(variables, trace_id)` 接口，支持子类声明版本号与元数据，便于提示词灰度与效果归因。

## 扩展自定义 Prompt 实体

```python
from react_ai.prompts.base import BasePrompt

class SqlAuditPrompt(BasePrompt):
    name = "sql_audit"
    version = "1.2.0"

    def build_messages(self, variables: dict) -> list[PromptMessage]:
        return [PromptMessage(role="system", content=f"审查以下 SQL: {variables['sql']}")]

pm.register_prompt(SqlAuditPrompt())
```
