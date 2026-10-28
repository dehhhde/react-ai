"""Agent 运行时状态与配置模型."""
from typing import Any, Dict, List, Optional
from pydantic import BaseModel, Field
from react_ai.schemas.messages import ChatMessage, UsageStats


class AgentConfig(BaseModel):
    name: str = "EnterpriseReActAgent"
    description: str = "企业级标准 ReAct 智能体"
    max_steps: int = 15
    timeout_seconds: float = 120.0
    temperature: float = 0.2
    enable_stream: bool = True
    stop_words: List[str] = Field(default_factory=list)
    system_prompt_override: Optional[str] = None


class StepResult(BaseModel):
    step_number: int
    thought: Optional[str] = None
    action_name: Optional[str] = None
    action_input: Optional[Dict[str, Any]] = None
    observation: Optional[str] = None
    is_final: bool = False
    final_answer: Optional[str] = None


class AgentResponse(BaseModel):
    session_id: str
    run_id: str
    final_output: str
    steps: List[StepResult] = Field(default_factory=list)
    usage: UsageStats = Field(default_factory=UsageStats)
    success: bool = True
    error_message: Optional[str] = None
