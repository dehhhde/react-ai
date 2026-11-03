"""受限安全命令行执行工具.

安全策略（企业级最小集）：
- 白名单可执行文件：仅允许 ALLOWED_COMMANDS 中登记的命令，杜绝任意命令执行
- 参数数组化：禁用 shell=True，subprocess 接收 argv 列表，避免命令注入
- 元字符拦截：拦截管道、重定向、命令替换、变量展开等 shell 特殊字符
- 输出截断：stdout/stderr 均限制最大长度，防止撑爆 LLM 上下文
- 环境隔离：清空继承环境变量，仅保留最小白名单；限制工作目录
- 资源上限：超时由沙箱统一控制（timeout_seconds），此处不再重复设置
"""
import os
import shlex
import subprocess
from typing import Any, Dict, List

from pydantic import Field

from react_ai.tools.base import BaseTool, ToolContext, ToolInputModel


class SafeBashInput(ToolInputModel):
    command: str = Field(description="要执行的指令（仅支持白名单命令，不允许管道/重定向）")


class SafeBashTool(BaseTool):
    name = "bash"
    description = (
        "在受限沙箱中执行受控系统指令。仅允许白名单命令，禁止 shell 元字符（管道/重定向/命令替换）。"
    )
    side_effects = "destructive"
    tags = ["system", "bash"]
    requires_approval = True
    input_model = SafeBashInput

    # 允许执行的命令白名单：command -> 描述（仅含只读/安全命令，可按需扩展）
    # 注意：find 因支持 -delete/-exec 等破坏性动作，不纳入白名单
    ALLOWED_COMMANDS: Dict[str, str] = {
        "ls": "列出目录内容",
        "cat": "查看文件内容",
        "pwd": "显示当前工作目录",
        "echo": "回显文本",
        "head": "查看文件前 N 行",
        "tail": "查看文件后 N 行",
        "grep": "在文件中搜索匹配行",
        "wc": "统计行数/词数/字节数",
        "stat": "查看文件元数据",
    }

    # 禁止出现在参数中的 shell 元字符（管道、重定向、命令替换、变量等）
    FORBIDDEN_META = set('|&;<>`$(){}[]"\'\\')

    # 输出最大字节数（stdout + stderr 各自独立限制）
    MAX_OUTPUT_BYTES = 8 * 1024

    def execute(self, arguments: dict, ctx: ToolContext) -> Any:
        raw = arguments.get("command", "").strip()
        if not raw:
            raise PermissionError("指令不能为空")

        # 1. 解析为 argv 数组（shlex 正确处理引号），禁用 shell=True
        try:
            argv: List[str] = shlex.split(raw)
        except ValueError as exc:
            raise PermissionError(f"指令解析失败: {exc}")

        if not argv:
            raise PermissionError("指令不能为空")

        cmd_name = argv[0]
        # 2. 白名单校验：仅允许登记的命令
        if cmd_name not in self.ALLOWED_COMMANDS:
            raise PermissionError(
                f"命令 [{cmd_name}] 不在白名单中，已被安全拦截。"
                f"允许的命令: {', '.join(self.ALLOWED_COMMANDS.keys())}"
            )

        # 3. 元字符拦截：禁止管道、重定向、命令替换等
        #    注意：shlex 已将带引号的整体作为单 token，但仍需防未转义的元字符参数
        for token in argv:
            if any(ch in self.FORBIDDEN_META for ch in token):
                raise PermissionError(
                    f"参数包含被禁止的 shell 元字符，已拦截: {token!r}"
                )

        # 4. 环境隔离：清空继承环境，仅保留最小集合
        safe_env = {
            "PATH": "/usr/bin:/bin",
            "LANG": "C.UTF-8",
            "HOME": "/tmp",
        }

        # echo 是 shell 内建命令，跨平台无独立可执行文件，直接在 Python 中实现以避免 shell=True
        if cmd_name == "echo":
            output = " ".join(argv[1:])
            return {
                "stdout": output + "\n",
                "stderr": "",
                "exit_code": 0,
                "truncated": False,
            }

        # 5. 限制工作目录为临时目录，避免污染宿主
        cwd = os.environ.get("SAFE_BASH_WORKDIR", "/tmp")

        try:
            result = subprocess.run(
                argv,
                shell=False,               # 关键：数组化执行，杜绝 shell 注入
                capture_output=True,
                text=True,
                env=safe_env,
                cwd=cwd,
                check=False,
            )
        except FileNotFoundError:
            raise PermissionError(f"命令 [{cmd_name}] 在受限环境中不可用")

        # 6. 输出截断，防止大结果撑爆上下文
        stdout = self._truncate(result.stdout)
        stderr = self._truncate(result.stderr)

        return {
            "stdout": stdout,
            "stderr": stderr,
            "exit_code": result.returncode,
            "truncated": len(result.stdout) > self.MAX_OUTPUT_BYTES
            or len(result.stderr) > self.MAX_OUTPUT_BYTES,
        }

    def _truncate(self, text: str) -> str:
        if not text:
            return ""
        if len(text) > self.MAX_OUTPUT_BYTES:
            return text[: self.MAX_OUTPUT_BYTES] + "\n...[output truncated]"
        return text
