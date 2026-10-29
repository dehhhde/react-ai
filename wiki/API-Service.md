# API Service 服务接入层

目录：`api/`。基于 FastAPI 暴露 REST 与 SSE 端点，应用入口 [api/main.py](file:///d:/code/react-ai/api/main.py)，路由 [api/routes/agent.py](file:///d:/code/react-ai/api/routes/agent.py)。

## 启动

```bash
python -m uvicorn api.main:app --host 0.0.0.0 --port 8000 --reload
# 或 python -m api.main（内置 uvicorn.run, reload=True）
```

Swagger UI：`http://localhost:8000/docs`。

## 应用装配

- `FastAPI(title="react-ai Enterprise Agent API", version="0.1.0")`
- 中间件：`CORSMiddleware`（当前 `allow_origins=["*"]`，生产需收紧）
- 模块级单例：`ToolRegistry`（注册 `KnowledgeRetrievalTool` + 9 个售后业务工具）与 `ReActAgent`（`create_llm_client()` 按 `.env` 装配默认模型）

## 端点

### GET /health

K8s 存活/就绪探针：

```json
{"status": "ok", "service": "react-ai"}
```

### POST /api/v1/agent/chat

同步阻塞对话，内部调用 `agent.run()`，返回聚合 `AgentResponse`：

```json
{
  "query": "查询订单 ORD-1001 的物流",
  "session_id": "optional-session",
  "stream": true
}
```

响应字段：`session_id`、`run_id`、`final_output`、`steps[]`（每步 thought/action/observation）、`usage`、`success`、`error_message`。

### POST /api/v1/agent/chat/stream

SSE 流式对话（`media_type="text/event-stream"`）。每个 `AgentEvent` 序列化为一帧：

```text
data: {"event_id":"...","event_type":"tool_start","step":1,"payload":{"tool":"query_logistics","args":{...}},"timestamp":...}

data: {"event_id":"...","event_type":"run_end","step":2,"payload":{"output":"...","steps":[...]},"timestamp":...}
```

`event_type` 取值见 [Core Engine](Core-Engine.md) 事件协议表。前端消费示例：

```javascript
const es = new EventSource("/api/v1/agent/chat/stream"); // 实际为 POST 时改用 fetch + ReadableStream
for await (const chunk of stream) {
  const event = JSON.parse(chunk.replace("data: ", ""));
  switch (event.event_type) {
    case "thought_start": renderThinking(event.step); break;
    case "tool_start":    renderToolCard(event.payload.tool, event.payload.args); break;
    case "run_end":       renderAnswer(event.payload.output); break;
  }
}
```

### GET /api/v1/agent/tools

返回 `registry.list_definitions()`——全部已注册工具的 `ToolDefinition`（名称、描述、参数 JSON Schema、审批/超时元数据）。

## 请求模型

```python
class ChatRequest(BaseModel):
    query: str
    session_id: str | None = None   # 缺省 "default-session"
    stream: bool = True
```

## 部署注意

1. **会话隔离**：当前 `session_id` 传入 `RunContext`，但消息记忆尚未接入 API 链路（每次请求独立上下文）；多轮对话需接入 [Memory](Memory.md) 层。
2. **并发模型**：`agent` 为进程级单例，`ReActAgent` 每次调用创建独立 `AgentState`，可安全并发；LLM 连接复用依赖各 Provider 的 httpx 客户端。
3. **护栏织入**：请求/响应护栏需在路由层显式调用 `GuardrailChain`（见 [Guardrails](Guardrails.md)）。
