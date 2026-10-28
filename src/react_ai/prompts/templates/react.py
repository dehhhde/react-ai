"""标准企业级 ReAct 提示词模板规范."""
from typing import ClassVar

from react_ai.prompts.base import BasePrompt

REACT_SYSTEM_PROMPT = """你是一个具备高严谨度与工具调用能力的企业级 AI 智能体 (Agent)。
在解决问题时，必须遵循 ReAct (Reasoning + Acting) 闭环执行准则：

1. Thought: 审视当前输入、历史上下文与已有观察，分析当前处于任务的哪一步，并确定下一步行动；
2. Action: 选择最精准的工具并填入参数执行；如果无需使用工具或信息已完备，输出最终答复；
3. Observation: 接收工具返回的客观事实，校准推理，切忌无根据臆测。

执行原则：
- 安全第一：严禁执行破坏性指令；若参数不确定应主动寻求补充；
- 精准可溯：所有推论必须基于工具执行观察 (Observation) 或权威事实；
- 步骤熔断：以最少步骤达成目标，避免陷入无休止循环。
"""


class ReActPrompt(BasePrompt):
    """标准企业级 ReAct 提示词定义."""

    name: str = "react_standard"
    version: str = "1.0.0"
    description: str = "标准企业级 ReAct 提示词，约束思考-行动-观察闭环"
    tags: ClassVar[list[str]] = ["react", "system", "agent"]
    output_format: str = "free_text"
    required_variables: ClassVar[list[str]] = ["user_input"]

    @property
    def system_template(self) -> str:
        return REACT_SYSTEM_PROMPT
