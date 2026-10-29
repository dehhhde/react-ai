# LLM Gateway 多模型网关

模块：`react_ai.llm`。屏蔽各厂商协议差异，提供统一推理接口与主备容灾降级。

## BaseLLMClient 抽象接口

文件：[llm/base.py](file:///d:/code/react-ai/src/react_ai/llm/base.py)

```python
class LLMResponse(BaseModel):
    content: str | None = None            # 文本答复
    tool_calls: list[ToolCall] | None     # Function Calling 决策
    usage: UsageStats                     # Token 消耗
    raw_response: dict | None             # 厂商原始响应

class BaseLLMClient(ABC):
    @abstractmethod
    async def generate(
        self,
        messages: list[ChatMessage],
        tools: list[ToolDefinition] | None = None,
        temperature: float = 0.2,
        max_tokens: int | None = None,
    ) -> LLMResponse: ...

    @abstractmethod
    async def stream_generate(
        self,
        messages: list[ChatMessage],
        tools: list[ToolDefinition] | None = None,
        temperature: float = 0.2,
    ) -> AsyncGenerator[str, None]: ...
```

## 工厂函数 create_llm_client

文件：[llm/client.py](file:///d:/code/react-ai/src/react_ai/llm/client.py)

```python
def create_llm_client(provider: str | None = None, **kwargs) -> BaseLLMClient
```

- `provider` 缺省时取 `settings.llm.default_provider`（默认 `deepseek`）。
- 分支：`deepseek` → `DeepSeekProvider`；`anthropic` → `AnthropicProvider`；其余（如 `openai`）→ `OpenAIProvider`。
- `api_key` / `api_base` / `model` 未显式传入时回落到 `settings.llm` 配置。

```python
llm = create_llm_client("openai", model="gpt-4o", api_key="sk-...")
```

## Providers（厂商适配器）

目录：[llm/providers/](file:///d:/code/react-ai/src/react_ai/llm/providers)。三个适配器均实现 `generate` / `stream_generate`，将厂商响应归一化为 `LLMResponse`（含把厂商 tool_calls 结构映射为统一 `ToolCall`）：

| Provider | 文件 | 目标服务 |
|---|---|---|
| `OpenAIProvider` | `providers/openai.py` | OpenAI 及 OpenAI 兼容网关（含 `api_base` 自定义） |
| `DeepSeekProvider` | `providers/deepseek.py` | DeepSeek V3/R1 |
| `AnthropicProvider` | `providers/anthropic.py` | Anthropic Claude 系列 |

另有 [llm/base_model_client.py](file:///d:/code/react-ai/src/react_ai/llm/base_model_client.py) 提供基于 LangChain `BaseChatModel` 的通用适配层（供 LangGraph 集成示例使用，见 [examples/aftersales_langgraph.py](../examples/aftersales_langgraph.py)）。

## LLMRouter 容灾降级

文件：[llm/router.py](file:///d:/code/react-ai/src/react_ai/llm/router.py)。`LLMRouter` 本身也是 `BaseLLMClient`，可直接注入 `ReActAgent`：

```python
router = LLMRouter(
    primary=create_llm_client("deepseek"),
    fallbacks=[create_llm_client("openai", model="gpt-4o")],
)
agent = ReActAgent(llm=router, tool_registry=registry)
```

降级语义：

- `generate()`：按 `[primary] + fallbacks` 顺序尝试，任一成功即返回；全部失败抛 `RuntimeError("全部 LLM 节点推理均失败，末次异常: ...")`。
- `stream_generate()`：同样顺序尝试，节点中途失败则切换到下一节点继续输出。

## 扩展新厂商

继承 `BaseLLMClient` 实现两个抽象方法即可接入全框架（Agent、Router、测试 Mock 通用）：

```python
class MyProvider(BaseLLMClient):
    async def generate(self, messages, tools=None, temperature=0.2, max_tokens=None):
        ...  # 调用厂商 API，返回 LLMResponse
    async def stream_generate(self, messages, tools=None, temperature=0.2):
        ...  # yield 文本增量
```
