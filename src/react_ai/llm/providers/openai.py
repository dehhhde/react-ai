"""OpenAI 兼容协议适配器 (支持真实 HTTP 请求与零 Key 离线演示模拟)."""
import os
import json
import httpx
from typing import AsyncGenerator, Dict, List, Optional
from react_ai.llm.base import BaseLLMClient, LLMResponse
from react_ai.schemas.messages import ChatMessage, MessageRole, ToolCall, UsageStats
from react_ai.schemas.tools import ToolDefinition


class OpenAIProvider(BaseLLMClient):
    def __init__(
        self,
        api_key: Optional[str] = None,
        api_base: Optional[str] = None,
        model: str = "gpt-4o",
    ):
        self.api_key = api_key or os.getenv("OPENAI_API_KEY")
        self.api_base = (api_base or os.getenv("OPENAI_BASE_URL") or "https://api.openai.com/v1").rstrip("/")
        self.model = model

    async def generate(
        self,
        messages: List[ChatMessage],
        tools: Optional[List[ToolDefinition]] = None,
        temperature: float = 0.2,
        max_tokens: Optional[int] = None,
    ) -> LLMResponse:
        # 1. 若配置了 API Key，发起真实的 HTTP 接口调用
        if self.api_key:
            return await self._call_real_api(messages, tools, temperature, max_tokens)

        # 2. 离线无 Key 模式：提供逼真的 ReAct 多步推理与工具调用演示
        return self._simulate_react_step(messages, tools)

    async def _call_real_api(
        self,
        messages: List[ChatMessage],
        tools: Optional[List[ToolDefinition]] = None,
        temperature: float = 0.2,
        max_tokens: Optional[int] = None,
    ) -> LLMResponse:
        url = f"{self.api_base}/chat/completions"
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

        formatted_messages = []
        for m in messages:
            msg_dict: Dict[str, Any] = {"role": m.role.value, "content": m.content or ""}
            if m.tool_calls:
                msg_dict["tool_calls"] = [
                    {
                        "id": tc.id,
                        "type": "function",
                        "function": {"name": tc.name, "arguments": json.dumps(tc.arguments)},
                    }
                    for tc in m.tool_calls
                ]
            if m.tool_call_id:
                msg_dict["tool_call_id"] = m.tool_call_id
            formatted_messages.append(msg_dict)

        payload = {
            "model": self.model,
            "messages": formatted_messages,
            "temperature": temperature,
        }
        if max_tokens:
            payload["max_tokens"] = max_tokens
        if tools:
            payload["tools"] = [
                {
                    "type": "function",
                    "function": {
                        "name": t.name,
                        "description": t.description,
                        "parameters": t.parameters.model_dump(),
                    },
                }
                for t in tools
            ]

        async with httpx.AsyncClient(timeout=60.0) as client:
            resp = await client.post(url, headers=headers, json=payload)
            resp.raise_for_status()
            data = resp.json()

        choice = data["choices"][0]["message"]
        tool_calls = None
        if "tool_calls" in choice and choice["tool_calls"]:
            tool_calls = [
                ToolCall(
                    id=tc["id"],
                    name=tc["function"]["name"],
                    arguments=json.loads(tc["function"]["arguments"]),
                )
                for tc in choice["tool_calls"]
            ]

        usage_data = data.get("usage", {})
        usage = UsageStats(
            prompt_tokens=usage_data.get("prompt_tokens", 0),
            completion_tokens=usage_data.get("completion_tokens", 0),
            total_tokens=usage_data.get("total_tokens", 0),
        )

        return LLMResponse(
            content=choice.get("content"),
            tool_calls=tool_calls,
            usage=usage,
            raw_response=data,
        )

    def _simulate_react_step(
        self,
        messages: List[ChatMessage],
        tools: Optional[List[ToolDefinition]] = None,
    ) -> LLMResponse:
        # 寻找最新的 Tool 结果是否已注入
        has_tool_observation = any(m.role == MessageRole.TOOL for m in messages)

        if not has_tool_observation and tools:
            # 步骤 1：决策调用已注册的工具
            tool_to_use = tools[0]
            # 提取最后一条用户提问中的数学表达式或关键词
            last_user_msg = next((m.content for m in reversed(messages) if m.role == MessageRole.USER), "")
            expression = "(35 * 12) + 180" if "(35 * 12)" in last_user_msg else "10 + 20"

            return LLMResponse(
                content=f"为了回答问题，我需要使用工具 [{tool_to_use.name}] 计算表达式。",
                tool_calls=[
                    ToolCall(
                        id="call_mock_101",
                        name=tool_to_use.name,
                        arguments={"expression": expression} if tool_to_use.name == "calculate" else {"query": last_user_msg},
                    )
                ],
                usage=UsageStats(prompt_tokens=85, completion_tokens=25, total_tokens=110),
            )
        else:
            # 步骤 2：收到 Observation 后的最终答复
            latest_tool_msg = next((m.content for m in reversed(messages) if m.role == MessageRole.TOOL), "完成")
            return LLMResponse(
                content=f"根据工具执行观察结果（输出：{latest_tool_msg}），最终计算结论为 {latest_tool_msg}。",
                tool_calls=None,
                usage=UsageStats(prompt_tokens=120, completion_tokens=35, total_tokens=155),
            )

    async def stream_generate(
        self,
        messages: List[ChatMessage],
        tools: Optional[List[ToolDefinition]] = None,
        temperature: float = 0.2,
    ) -> AsyncGenerator[str, None]:
        res = await self.generate(messages, tools, temperature)
        if res.content:
            yield res.content
