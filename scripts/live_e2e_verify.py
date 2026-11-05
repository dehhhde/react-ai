"""§2.2 真实链路端到端验证驱动。

以「单轮」为粒度驱动 examples/aftersales_langgraph.py 的售后图，使同一 thread_id 的
连续轮次可以落在不同进程上，从而验证 PG 检查点的跨重启续跑与多实例状态一致；
同时旁路统计 function calling 原生 tool_calls 命中与正文 JSON 回退比例。

用法：
  py scripts/live_e2e_verify.py turn --user USR_001 --session E2E_C001 \
      --msg "我买的衣服破了，要退货退款，订单号是 ORD_CLOTH_001" \
      --label C001 --stats /tmp/e2e_stats.jsonl

§3.2 中途换意图与流程挂起：同一 --session 逐轮调用即可，结果里的 flow_action/pending_flows
就是挂起栈的出入栈轨迹（每轮一个进程，栈状态由 PG 检查点续跑）：
  turn --session E2E_F001 --msg "鞋子有瑕疵要退货退款，订单号 ORD_CLOTH_001"   # 缺原因大类 → 追问
  turn --session E2E_F001 --msg "还是换货吧"                                 # flow_action=switch，RETURN_AND_REFUND 入栈
  turn --session E2E_F001 --msg "继续退货"                                   # flow_action=resume，快照槽位与轮次一起还原
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import selectors
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for _p in (str(ROOT), str(ROOT / "examples")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

try:  # 让 Windows 控制台直接输出中文，免去调用方设置环境变量
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")  # §2.3 起节点轨迹走 stderr 结构化日志
except Exception:
    pass

from dotenv import load_dotenv  # noqa: E402

load_dotenv(ROOT / ".env")

import aftersales_langgraph as demo  # noqa: E402

# ---------------------------------------------------------------- 抽槽来源探针
_TRACE: dict = {"resp": None}  # 暂存最近一次 LLM 响应形态，供分类使用


def _install_probe() -> None:
    """包装 LLM 路由器与降级函数，记录每次抽槽走的结构路径，不改变任何行为。"""
    orig_get_router = demo.get_llm_router
    orig_fallback = demo.fallback_extract_slots

    class ProbedRouter:
        def __init__(self, real):
            self._real = real

        async def generate(self, *args, **kwargs):
            resp = await self._real.generate(*args, **kwargs)
            tool_args = None
            if getattr(resp, "tool_calls", None):
                raw = resp.tool_calls[0].arguments
                tool_args = raw if isinstance(raw, dict) else demo._extract_json(str(raw))
            _TRACE["resp"] = {
                "has_tool_calls": bool(getattr(resp, "tool_calls", None)),
                "tool_args_hit_slots": bool(tool_args and set(tool_args) & demo._SLOT_KEYS),
                "content_json_ok": bool(demo._extract_json(resp.content or "")) if not (
                    tool_args and set(tool_args) & demo._SLOT_KEYS) else False,
                "content_len": len(resp.content or ""),
            }
            return resp

    def get_router_probed():
        return ProbedRouter(orig_get_router())

    def fallback_probed(user_msg, active_intent=None):
        out = orig_fallback(user_msg, active_intent)
        _TRACE["degraded"] = True
        return out

    demo.get_llm_router = get_router_probed
    demo.fallback_extract_slots = fallback_probed


_orig_extract = demo.extract_slots_via_llm


async def _traced_extract(messages, thread_id: str = "", flow_context: str = ""):
    _TRACE["resp"] = None
    _TRACE["degraded"] = False
    started = time.perf_counter()
    out = await _orig_extract(messages, thread_id, flow_context)
    resp = _TRACE.get("resp") or {}
    if out is None:
        source = "keyword_fallback" if _TRACE.get("degraded") else "rejected_or_error"
    elif resp.get("tool_args_hit_slots"):
        source = "tool_calls"
    elif resp.get("content_json_ok"):
        source = "content_json"
    else:
        source = "unknown"
    return {
        "source": source,
        "latency_ms": round((time.perf_counter() - started) * 1000, 1),
        "has_tool_calls": resp.get("has_tool_calls"),
        "flow_context": bool(flow_context),  # 本轮是否向模型交代了会话中流程（§3.2 换意图判定的前置条件）
        "slots": out,
    }


async def _run_turn(user_id: str, session_id: str, message: str, thread_id: str) -> dict:
    """执行一轮完整图调用，返回业务结果与本轮抽槽轨迹。"""
    calls = []

    async def extract_wrap(msgs, thread_id_: str = "", flow_context: str = ""):
        rec = await _traced_extract(msgs, thread_id_, flow_context)
        calls.append(rec)
        return rec["slots"]

    demo.extract_slots_via_llm = extract_wrap
    async with demo.open_checkpointer() as checkpointer:
        app = demo.build_aftersales_graph(checkpointer=checkpointer)
        # 经 §2.3 的标准入口执行：同 thread 串行化 + 检查点分叉检测，节点日志携带 thread_id
        state = await demo.ainvoke_serialized(
            app,
            {"session_id": session_id, "user_id": user_id, "user_message": message},
            {"configurable": {"thread_id": thread_id}},
        )
    return {
        "thread_id": thread_id,
        "pid": os.getpid(),
        "user_message": message,
        "intent": state.get("intent"),
        "order_id": state.get("order_id"),
        "reason_type": state.get("reason_type"),
        "missing_slots": state.get("missing_slots"),
        "slot_rounds": state.get("slot_rounds"),
        "slot_error": state.get("slot_error"),
        "decision": state.get("decision"),
        "flow_action": state.get("flow_action"),                 # 本轮与会话中流程的关系（§3.2）
        "flow_status": state.get("flow_status"),
        "flow_turn_total": state.get("flow_turn_total"),
        "pending_flows": [{"flow_id": p.get("flow_id"), "intent": p.get("intent"),
                           "slot_rounds": p.get("slot_rounds")}
                          for p in (state.get("pending_flows") or [])],  # 挂起栈快照（§3.2）
        "final_reply": state.get("final_reply"),
        "dialog_turns": len(state.get("dialog_history") or []),
        "extraction_calls": calls,
        "metrics_counters": demo.metrics.snapshot()["counters"],  # 本轮 §2.3 指标增量（进程内计数）
    }


def _run_async(coro):
    """统一事件循环入口：Windows 下 psycopg3 异步需 Selector 循环（与主示例一致）。"""
    if sys.platform != "win32":
        return asyncio.run(coro)
    if sys.version_info >= (3, 12):                    # loop_factory 是 3.12 才加入的 asyncio.run 参数
        return asyncio.run(coro, loop_factory=lambda: asyncio.SelectorEventLoop(selectors.SelectSelector()))
    loop = asyncio.SelectorEventLoop(selectors.SelectSelector())  # 3.10/3.11 只能手工建循环，否则脚本入口直接 TypeError
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


async def _run_fork_check(thread_id: str) -> dict:
    """读取该 thread 的 leaf 检查点数：>1 即历史分叉（§2.2 报告 §6.2 的缺陷特征）。"""
    import psycopg

    async with demo.open_checkpointer() as saver:
        backend = type(saver).__name__
        if backend == "MemorySaver":
            return {"thread_id": thread_id, "checkpointer": backend, "linear_history": None,
                    "note": "内存检查点无跨进程分叉风险，需挂 PG 才能验证"}
        conn = await psycopg.AsyncConnection.connect(demo._pg_conn_string(), autocommit=True)
        try:
            heads = await demo._count_thread_heads(conn, thread_id)
        finally:
            await conn.close()
        return {"thread_id": thread_id, "checkpointer": backend, "heads": heads,
                "linear_history": heads <= 1,
                "fork_metric_total": demo.metrics.counter("checkpoint_fork_total")}


def main() -> int:
    parser = argparse.ArgumentParser(description="售后真实链路端到端验证驱动（§2.2 / §2.3）")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_turn = sub.add_parser("turn", help="在指定 thread_id 上执行一轮图调用")
    p_turn.add_argument("--user", required=True)
    p_turn.add_argument("--session", required=True)
    p_turn.add_argument("--msg", required=True)
    p_turn.add_argument("--thread", default=None, help="thread_id，默认 {tenant}:{user}:{session}")
    p_turn.add_argument("--label", default="", help="用例标记，写入统计便于归组")
    p_turn.add_argument("--stats", default=None, help="抽槽来源统计 JSONL 输出路径")
    p_turn.add_argument("--out", default=None, help="把本轮结果 JSON 写入该文件（节点日志仍走 stderr）")
    p_turn.add_argument("--quiet", action="store_true", help="只输出结果 JSON，透传日志静默")

    p_fork = sub.add_parser("fork-check", help="检查同一 thread_id 的检查点是否分叉（§2.3 会话串行化验收）")
    p_fork.add_argument("--thread", required=True, help="待检查的 thread_id")

    args = parser.parse_args()
    _install_probe()

    if args.cmd == "fork-check":
        payload = _run_async(_run_fork_check(args.thread))
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0 if payload.get("linear_history") else 1

    thread_id = args.thread or f"default:{args.user}:{args.session}"

    if not args.quiet:
        print(f"[turn] thread_id={thread_id} pid={os.getpid()} msg={args.msg}", file=sys.stderr)

    result = _run_async(_run_turn(args.user, args.session, args.msg, thread_id))

    for call in result["extraction_calls"]:
        rec = {"label": args.label, "thread_id": thread_id, "pid": result["pid"], **call}
        if args.stats:
            with open(args.stats, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
        else:
            print("[STATS] " + json.dumps(rec, ensure_ascii=False))

    payload = json.dumps(result, ensure_ascii=False, indent=2)
    if args.out:
        Path(args.out).write_text(payload, encoding="utf-8")
    else:
        print(payload)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
