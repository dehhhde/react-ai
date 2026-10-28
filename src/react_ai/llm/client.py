"""LLM 工厂与客户端快捷创建."""
from typing import Optional
from react_ai.config.settings import settings
from react_ai.llm.base import BaseLLMClient
from react_ai.llm.providers.openai import OpenAIProvider
from react_ai.llm.providers.deepseek import DeepSeekProvider
from react_ai.llm.providers.anthropic import AnthropicProvider
from react_ai.llm.router import LLMRouter


def create_llm_client(provider: Optional[str] = None, **kwargs) -> BaseLLMClient:
    selected_provider = (provider or settings.llm.default_provider or "deepseek").lower()
    
    if selected_provider == "deepseek":
        return DeepSeekProvider(
            api_key=kwargs.pop("api_key", settings.llm.api_key),
            api_base=kwargs.pop("api_base", settings.llm.api_base),
            model=kwargs.pop("model", settings.llm.model_name or "deepseek-chat"),
            **kwargs,
        )
    elif selected_provider == "anthropic":
        return AnthropicProvider(
            api_key=kwargs.pop("api_key", settings.llm.api_key),
            model=kwargs.pop("model", settings.llm.model_name),
            **kwargs,
        )
    else:
        return OpenAIProvider(
            api_key=kwargs.pop("api_key", settings.llm.api_key),
            api_base=kwargs.pop("api_base", settings.llm.api_base),
            model=kwargs.pop("model", settings.llm.model_name),
            **kwargs,
        )
