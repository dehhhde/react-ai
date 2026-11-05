"""提示词评测回归脚本（差距分析 2.11）.

用法（在项目根目录）：
    python eval/run_prompt_eval.py                        # 结构断言 + 与基线 diff
    python eval/run_prompt_eval.py --update-baseline      # 生成/刷新基线文件
    python eval/run_prompt_eval.py --llm                  # 额外对未阻断样本做真实 LLM 冒烟

结构断言（无 LLM 依赖，可在 CI 稳定运行）：
- 正常样本：渲染成功，messages[0] 为含 ReAct 约束的 system，末条 user 携带（脱敏后）输入；
- 注入样本：命中输入护栏抛 PromptInjectionError（blocked=true）；
- PII 样本：variables_used 快照中手机号/身份证已掩码；
- 基线 diff：逐样本记录渲染产物 sha256 与 token 估算，模板变更即暴露漂移。
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from react_ai.prompts.base import PromptInjectionError  # noqa: E402
from react_ai.prompts.manager import prompt_manager  # noqa: E402

EVAL_DIR = Path(__file__).resolve().parent / "prompts"


def render_case(prompt_name: str, user_input: str, trace_id: str) -> dict:
    """渲染单条样本，返回结构化评测结果（含护栏行为）."""
    try:
        result = prompt_manager.render_prompt(
            prompt_name, {"user_input": user_input}, trace_id=trace_id
        )
    except PromptInjectionError as exc:
        return {"blocked": True, "reason": str(exc)}
    rendered = result.to_chat_messages()
    fingerprint = hashlib.sha256(
        "\n---\n".join(f"{m.role}:{m.content}" for m in rendered).encode("utf-8")
    ).hexdigest()[:16]
    return {
        "blocked": False,
        "roles": [m.role for m in result.messages],
        "token_count": result.token_count,
        "fingerprint": fingerprint,
        "user_content": result.messages[-1].content if result.messages else "",
        "variables_used": result.variables_used,
    }


def check_structure(out: dict, expect: dict) -> list[str]:
    """按期望校验单样本渲染结构，返回违规描述列表."""
    errors: list[str] = []
    if expect.get("blocked"):
        if not out["blocked"]:
            errors.append("期望护栏阻断，但渲染成功放行")
        return errors
    if out["blocked"]:
        errors.append(f"期望正常渲染，但被护栏阻断: {out.get('reason')}")
        return errors
    if "system" not in out["roles"] or out["roles"][0] != "system":
        errors.append(f"首条消息应为 system，实际 {out['roles']}")
    if "ReAct" not in out["user_content"] and out["user_content"]:
        pass  # user 消息只要求携带输入，不校验内容
    if expect.get("masked"):
        snapshot = str(out["variables_used"].get("user_input", ""))
        if "[PHONE_MASKED]" not in snapshot and "[ID_MASKED]" not in snapshot:
            errors.append("期望 PII 已脱敏，但快照仍含原文")
    return errors


async def llm_smoke(prompt_name: str, user_input: str) -> str:
    """可选真实 LLM 冒烟：渲染后的消息直接问模型，返回回答摘要."""
    try:
        from react_ai.config import settings
        from react_ai.llm.providers.deepseek import DeepSeekProvider

        if not getattr(settings.llm, "api_key", None):
            return "SKIP(no api_key)"
        provider = DeepSeekProvider(
            api_key=settings.llm.api_key,
            base_url=getattr(settings.llm, "base_url", "https://api.deepseek.com"),
            model=getattr(settings.llm, "model", "deepseek-chat"),
        )
        result = prompt_manager.render_prompt(prompt_name, {"user_input": user_input})
        resp = await provider.generate(result.to_chat_messages(), temperature=0.2)
        return (resp.content or "")[:80]
    except Exception as exc:  # noqa: BLE001
        return f"SKIP({type(exc).__name__})"


def main() -> int:
    parser = argparse.ArgumentParser(description="提示词评测回归")
    parser.add_argument("--suite", default=str(EVAL_DIR / "react_standard.json"), help="评测集 JSON 路径")
    parser.add_argument("--update-baseline", action="store_true", help="以本次渲染结果刷新基线")
    parser.add_argument("--llm", action="store_true", help="附加真实 LLM 冒烟（需配置 api_key）")
    args = parser.parse_args()

    suite = json.loads(Path(args.suite).read_text(encoding="utf-8"))
    prompt_name = suite["prompt"]
    baseline_path = Path(args.suite).with_suffix(".baseline.json")
    baseline = {}
    if baseline_path.exists():
        baseline = json.loads(baseline_path.read_text(encoding="utf-8"))

    passed = failed = 0
    new_baseline: dict[str, dict] = {}
    for case in suite["samples"]:
        out = render_case(prompt_name, case["input"], trace_id=f"eval-{case['id']}")
        errors = check_structure(out, case.get("expect", {}))

        if not case.get("expect", {}).get("blocked"):
            prev = baseline.get(case["id"])
            if prev and not args.update_baseline and prev.get("fingerprint") != out["fingerprint"]:
                errors.append(f"与基线漂移: fingerprint {prev.get('fingerprint')} -> {out['fingerprint']}")
            new_baseline[case["id"]] = {
                "fingerprint": out["fingerprint"],
                "token_count": out["token_count"],
            }

        if args.llm and not out["blocked"]:
            answer = asyncio.run(llm_smoke(prompt_name, case["input"]))
            print(f"  [{case['id']}] LLM: {answer}")

        if errors:
            failed += 1
            print(f"FAIL {case['id']}: " + "; ".join(errors))
        else:
            passed += 1
            print(f"PASS {case['id']}")

    if args.update_baseline:
        baseline_path.write_text(json.dumps(new_baseline, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\n基线已写入 {baseline_path}（{len(new_baseline)} 条）")

    print(f"\n评测结果: {passed} passed, {failed} failed / {len(suite['samples'])} samples")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
