"""提示词模板管理器 (支持动态插值、版本化与 BasePrompt 实体管理)."""
from typing import Any

from jinja2 import Template

from react_ai.prompts.base import BasePrompt, PromptResult
from react_ai.prompts.templates.react import REACT_SYSTEM_PROMPT, ReActPrompt


class PromptManager:
    """提示词管理器，支持原生模板字符串与 BasePrompt 结构化实体."""

    def __init__(self):
        self._templates: dict[str, str] = {
            "react_system": REACT_SYSTEM_PROMPT,
        }
        self._prompts: dict[str, BasePrompt] = {
            "react_standard": ReActPrompt(),
        }

    def register(self, name: str, template_str: str) -> None:
        """注册原始模板字符串."""
        self._templates[name] = template_str

    def register_prompt(self, prompt: BasePrompt) -> None:
        """注册 BasePrompt 实体."""
        self._prompts[prompt.name] = prompt

    def get_prompt(self, name: str) -> BasePrompt | None:
        """获取已注册的 BasePrompt 实体."""
        return self._prompts.get(name)

    def render(self, name: str, variables: dict[str, Any]) -> str:
        """基于 Jinja2 渲染原始模板字符串."""
        raw = self._templates.get(name)
        if not raw:
            raise KeyError(f"未找到提示词模板: {name}")
        return Template(raw).render(**variables)

    def render_prompt(
        self,
        name: str,
        variables: dict[str, Any],
        trace_id: str | None = None,
    ) -> PromptResult:
        """根据名称获取并渲染 BasePrompt 实体."""
        prompt = self.get_prompt(name)
        if not prompt:
            raise KeyError(f"未找到提示词实体: {name}")
        return prompt.render(variables=variables, trace_id=trace_id)


prompt_manager = PromptManager()
