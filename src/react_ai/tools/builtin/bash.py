"""受限安全命令行执行工具."""
import subprocess
from typing import Any, Dict
from react_ai.tools.base import BaseTool, ToolContext


class SafeBashTool(BaseTool):
    name = "bash"
    description = "在受限沙箱中执行受控系统指令"
    side_effects = "destructive"
    tags = ["system", "bash"]
    requires_approval = True
    FORBIDDEN_CMDS = ["rm -rf", "format", "del /f", "mkfs"]

    @property
    def input_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "command": {"type": "string", "description": "要执行的 Shell 指令"}
            },
            "required": ["command"],
        }

    def execute(self, arguments: dict, ctx: ToolContext) -> Any:
        command = arguments.get("command", "")
        for forbidden in self.FORBIDDEN_CMDS:
            if forbidden in command:
                raise PermissionError(f"指令包含高危操作已被安全拦截: {forbidden}")

        result = subprocess.run(command, shell=True, capture_output=True, text=True, timeout=15)
        return {"stdout": result.stdout, "stderr": result.stderr, "exit_code": result.returncode}
