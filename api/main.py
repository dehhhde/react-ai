"""企业级 FastAPI 后端接入入口."""
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from api.routes.agent import router as agent_router

app = FastAPI(
    title="react-ai Enterprise Agent API",
    version="0.1.0",
    description="企业级 ReAct AI 智能体运行时 REST & SSE 服务端",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(agent_router)


@app.get("/health")
async def health_check():
    return {"status": "ok", "service": "react-ai"}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("api.main:app", host="0.0.0.0", port=8000, reload=True)
