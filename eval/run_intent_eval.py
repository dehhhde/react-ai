"""意图与槽位抽取回归评测（第二轮方案 §2.1）。

一条命令量化 intent_recognition_node 的抽槽质量，替代"改提示词靠感觉"的盲改：

    py eval/run_intent_eval.py                      # 自动选择评测路径（有 LLM_API_KEY 走真实模型，否则关键词降级下限基线）
    py eval/run_intent_eval.py --mode fallback      # 强制关键词降级路径
    py eval/run_intent_eval.py --mode llm           # 强制真实 LLM 路径
    py eval/run_intent_eval.py --record-baseline eval/baselines/llm_main.json   # 记录基线
    py eval/run_intent_eval.py --baseline eval/baselines/llm_main.json          # 与基线对比，列出退步用例
    py eval/run_intent_eval.py --fail-under 0.9     # CI 门禁：用例整体通过率低于阈值即非零退出
    py eval/run_intent_eval.py --no-confidence-gate --confidence-report eval/reports/confidence.md
                                                    # 置信度标定跑法（§3.1）：关闭拦截、只量自评分分布并给出阈值建议
    py eval/run_intent_eval.py --confidence-high 0.85 --confidence-low 0.55
                                                    # 临时改阈值验证影响（不改 .env，不影响基线口径可比性提示）

评测只跑意图节点（含输入护栏、LLM/降级抽槽、槽位合并、置信度阈值、越界意图网关、订单业务校验），
链路内无写操作工具调用，可安全反复执行。分层得分口径见 eval/README.md。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

EVAL_DIR = Path(__file__).resolve().parent
ROOT = EVAL_DIR.parent
DEFAULT_DATASET = EVAL_DIR / "datasets" / "intent_slots.jsonl"
DEFAULT_FIXTURE = EVAL_DIR / "datasets" / "order_fixture.json"

SLOT_KEYS = ("intent", "order_id", "reason_type")  # 参与打分的关键槽位（reason 为自由文本，不做严格比对）
BANDS = ("high", "mid", "mid_deferred", "low", "confirmed", "unrated", "not_gated", "out_of_scope", "guardrail_blocked")  # 置信度落段（§3.1）
FLOW_ACTIONS = ("continue", "switch", "resume", "abort", "off_domain")  # 本轮消息与会话中流程的关系（§3.2）
CONFIDENCE_LOW = 0.5   # 低置信阈值：运行时由被测模块的配置覆盖（可被 --confidence-low 临时改写做标定）
CONFIDENCE_HIGH = 0.8  # 高置信阈值：同上
PHONE_RE = re.compile(r"1[3-9]\d{9}")
ID_RE = re.compile(r"\d{17}[\dXx]")

# 订单业务校验失败文案：与 examples/aftersales_langgraph.py 的 validate_order_slot 保持逐字一致，
# 使 --db stub 与 --db live 两种模式下的 slot_error 断言等价。
MSG_NOT_FOUND = "未查询到该订单，请核对订单号（可在『我的订单』页面复制）后重新发送"
MSG_NOT_OWNER = "该订单不属于当前账号，请使用下单账号重新提交订单号，或回复『人工』"


def _load_module():
    """导入被测示例模块并把 src/examples 挂到 sys.path。"""
    for p in (str(ROOT / "src"), str(ROOT / "examples")):
        if p not in sys.path:
            sys.path.insert(0, p)
    import aftersales_langgraph as mod  # noqa: E402  被测模块
    return mod


def load_cases(path: Path) -> List[Dict[str, Any]]:
    cases: List[Dict[str, Any]] = []
    with path.open(encoding="utf-8") as fh:
        for lineno, line in enumerate(fh, 1):
            line = line.strip()
            if not line:
                continue
            try:
                cases.append(json.loads(line))
            except json.JSONDecodeError as exc:
                raise SystemExit(f"数据集第 {lineno} 行 JSON 非法: {exc}")
    seen: Dict[str, int] = {}
    for lineno, c in enumerate(cases, 1):
        cid = c.get("id")
        if not cid:
            raise SystemExit(f"数据集第 {lineno} 行缺少 id")
        if cid in seen:
            raise SystemExit(f"用例 id 重复: {cid}（第 {seen[cid]} 行与第 {lineno} 行）")
        seen[cid] = lineno
        expect = c.get("expect") or {}
        if not expect:
            raise SystemExit(f"{cid}: expect 为空，无任何断言项")
        unknown = set(expect) - {"intent", "intent_accept", "order_id", "reason_type", "missing_slots",
                                 "missing_include", "ask_user", "guardrail", "decision",
                                 "slot_error_contains", "mask_tokens", "confidence_min", "band",
                                 "flow_action", "pending_count", "pending_intents", "slot_rounds"}
        if unknown:
            raise SystemExit(f"{cid}: 未知断言字段 {sorted(unknown)}")
        if expect.get("intent") not in (None, "RETURN_AND_REFUND", "REFUND_ONLY", "EXCHANGE", "OTHER"):
            raise SystemExit(f"{cid}: expect.intent 取值非法 {expect.get('intent')}")
        if expect.get("guardrail") not in (None, "ALLOW", "MASK", "BLOCK"):
            raise SystemExit(f"{cid}: expect.guardrail 取值非法 {expect.get('guardrail')}")
        if expect.get("band") is not None and expect["band"] not in BANDS:
            raise SystemExit(f"{cid}: expect.band 取值非法 {expect['band']}，可选 {list(BANDS)}")
        if expect.get("flow_action") is not None and expect["flow_action"] not in FLOW_ACTIONS:
            raise SystemExit(f"{cid}: expect.flow_action 取值非法 {expect['flow_action']}，可选 {list(FLOW_ACTIONS)}")
        if expect.get("confidence_min") is not None and not 0 <= float(expect["confidence_min"]) <= 1:
            raise SystemExit(f"{cid}: expect.confidence_min 须在 0~1 之间")
    return cases


def _pending_snapshot(mod, item: Dict[str, Any]) -> Dict[str, Any]:
    """把用例里紧凑写法的挂起流程还原成 §3.2 的快照结构（复用被测模块自身的存档函数，防止两边字段漂移）。"""
    slot_values: Dict[str, Any] = {}
    for name, value in (item.get("slots") or {}).items():
        field = "value" if name in ("intent", "reason_type", "confidence") else "string_value"
        slot_values[name] = {field: value, "confidence": 0.9, "source": "评测预置"}
    mini_state = {"slot_rounds": item.get("slot_rounds", 0),
                  "order_validated_id": item.get("validated"),
                  "order_info": item.get("order_info"),
                  "dialog_history": []}
    return mod._flow_snapshot(mini_state, slot_values, item.get("missing") or [])


def build_state(case: Dict[str, Any], mod=None) -> Dict[str, Any]:
    """把标注用例还原为意图节点的输入状态（复刻真实多轮会话的 checkpoint 形态）。"""
    slot_values: Dict[str, Any] = {}
    for name, value in (case.get("seed_slots") or {}).items():
        field = "value" if name in ("intent", "reason_type", "confidence") else "string_value"
        slot_values[name] = {field: value, "confidence": 0.9, "source": "评测预置"}
    state: Dict[str, Any] = {
        "session_id": f"EVAL_{case['id']}",
        "user_id": case.get("user_id", "USR_001"),
        "user_message": case["utterance"],
        "dialog_history": list(case.get("context") or []),
        "slot_values": slot_values,
        "slot_rounds": 0,
    }
    if case.get("seed_pending") and mod is not None:      # §3.2 挂起栈：按被测模块的快照契约还原
        state["pending_flows"] = [_pending_snapshot(mod, item) for item in case["seed_pending"]]
    state.update(case.get("seed_state") or {})
    return state


def make_stub_validate(mod, fixture: Dict[str, Any]):
    """无 PG 环境下的确定性订单校验桩：只判存在性与归属权，与真实工具返回同构。"""
    orders = fixture["orders"]

    def stub(order_id: str, user_id: str):
        owner = orders.get(order_id)
        if owner is None:
            return MSG_NOT_FOUND, None
        if owner != user_id:
            return MSG_NOT_OWNER, None
        return None, {"order_id": order_id, "user_id": owner, "total_amount": 199.0, "items": [{"category": "CLOTHING"}]}

    return stub


def probe_db(mod) -> bool:
    """探测真实 PG 是否可用（用评测集内一个已知订单号做只读探针）。"""
    try:
        err, data = mod.validate_order_slot("ORD_CLOTH_001", "USR_001")
        return err is None and bool(data)
    except Exception:
        return False


def classify_guardrail(mod, case: Dict[str, Any], result: Dict[str, Any]) -> str:
    """判定本轮护栏动作：BLOCK 以节点短路产出的转人工决策为准，其余按护栏链实际动作归类。"""
    if result.get("decision") == "ESCALATE_HUMAN" and "护栏" in (result.get("human_reason") or ""):
        return "BLOCK"
    from react_ai.guardrails.base import GuardrailAction, GuardrailContext, GuardrailDirection

    check = mod.slot_guard_chain.check(
        case["utterance"],
        GuardrailContext(request_id=case["id"], session_id=f"EVAL_{case['id']}", direction=GuardrailDirection.INPUT),
    )
    return "MASK" if check.action == GuardrailAction.MODIFY else "ALLOW"


def evaluate_case(mod, case: Dict[str, Any], out: Dict[str, Any]) -> Dict[str, Any]:
    """比对单条用例：返回每个断言项的 actual/expected/pass。"""
    expect = case.get("expect") or {}
    merged = {**out["state"], **(out["result"] or {})}
    sv = merged.get("slot_values") or {}
    raw_confidence = (sv.get("confidence") or {}).get("value")
    actual = {
        "intent": (sv.get("intent") or {}).get("value"),
        "order_id": (sv.get("order_id") or {}).get("string_value"),
        "reason_type": (sv.get("reason_type") or {}).get("value"),
        "confidence": float(raw_confidence) if raw_confidence is not None else None,  # 本轮意图自评分（§3.1）
        "band": (out["result"] or {}).get("confidence_band"),                         # 阈值策略落段
        "missing_slots": list(merged.get("missing_slots") or []),
        "decision": merged.get("decision"),
        "slot_error": (out["result"] or {}).get("slot_error"),
        "guardrail": classify_guardrail(mod, case, out["result"] or {}),
        "ask_user": mod.route_slots(merged) == "ask_user",
        "sent_to_llm": _last_user_message(merged),
        "flow_action": (out["result"] or {}).get("flow_action"),                              # 本轮与会话中流程的关系（§3.2）
        "pending_count": len(merged.get("pending_flows") or []),                              # 挂起栈深度：用户已填信息是否还可挽回（§3.2）
        "pending_intents": [p.get("intent") for p in (merged.get("pending_flows") or [])],    # 栈内流程的意图，按挂起先后排列
        "slot_rounds": int(merged.get("slot_rounds") or 0),                                   # 该流程已消耗的补槽轮数（恢复时应一并还原）
    }
    checks: Dict[str, Dict[str, Any]] = {}

    def record(name: str, passed: bool, expected: Any, actual_value: Any) -> None:
        checks[name] = {"pass": passed, "expected": expected, "actual": actual_value}

    for slot in SLOT_KEYS:
        if slot not in expect:
            continue
        exp_val, act_val = expect[slot], actual[slot]
        accept = set(expect.get("intent_accept") or []) if slot == "intent" else set()
        if slot == "intent":
            # 意图允许等价结论（如"想退掉"判为退货退款或仅退款都算对）；弃权(null)仅当显式列入 intent_accept
            ok = act_val == exp_val or act_val in accept
        else:
            # 期望 None 表示"此处不得凭空产出"：反幻觉与坏订单号必须剔除
            ok = act_val == exp_val
        record(f"slot:{slot}", ok, exp_val, act_val)

    if "missing_slots" in expect:
        record("missing_slots", sorted(actual["missing_slots"]) == sorted(expect["missing_slots"]),
               sorted(expect["missing_slots"]), sorted(actual["missing_slots"]))
    if "missing_include" in expect:
        missing = set(actual["missing_slots"])
        record("missing_slots", all(s in missing for s in expect["missing_include"]),
               f"⊇ {expect['missing_include']}", sorted(missing))
    if "ask_user" in expect:
        record("ask_user", actual["ask_user"] == expect["ask_user"], expect["ask_user"], actual["ask_user"])
    if "guardrail" in expect:
        record("guardrail", actual["guardrail"] == expect["guardrail"], expect["guardrail"], actual["guardrail"])
    if "decision" in expect:
        record("decision", actual["decision"] == expect["decision"], expect["decision"], actual["decision"])
    if "slot_error_contains" in expect:
        record("slot_error", bool(actual["slot_error"]) and expect["slot_error_contains"] in actual["slot_error"],
               expect["slot_error_contains"], actual["slot_error"])
    if "confidence_min" in expect:  # 断言"这条明确表述模型应给出高自评分"——阈值标定的正向锚点
        conf = actual["confidence"]
        record("confidence_min", conf is not None and conf >= float(expect["confidence_min"]),
               expect["confidence_min"], conf)
    if "band" in expect:            # 断言阈值策略落段——直接验证 §3.1 三段收敛是否按配置生效
        record("band", actual["band"] == expect["band"], expect["band"], actual["band"])
    if "flow_action" in expect:     # 断言本轮会话关系——§3.2 换意图/恢复/取消是否按契约判定
        record("flow_action", actual["flow_action"] == expect["flow_action"],
               expect["flow_action"], actual["flow_action"])
    if "pending_count" in expect:   # 断言挂起栈深度：切换必须留痕，否则等于静默丢掉用户已填的信息
        record("pending_count", actual["pending_count"] == int(expect["pending_count"]),
               expect["pending_count"], actual["pending_count"])
    if "pending_intents" in expect:
        record("pending_intents", actual["pending_intents"] == list(expect["pending_intents"]),
               expect["pending_intents"], actual["pending_intents"])
    if "slot_rounds" in expect:     # 断言轮次预算归属：换意图归零、恢复时按快照还原
        record("slot_rounds", actual["slot_rounds"] == int(expect["slot_rounds"]),
               expect["slot_rounds"], actual["slot_rounds"])
    if "mask_tokens" in expect:
        sent = actual["sent_to_llm"] or ""
        tokens_ok = all(tok in sent for tok in expect["mask_tokens"])
        leaked = [m.group(0) for m in ID_RE.finditer(sent)] + [m.group(0) for m in PHONE_RE.finditer(sent)]
        record("pii_masked", tokens_ok and not leaked, f"{expect['mask_tokens']} 且无原始号码",
               {"masked_tokens": [t for t in expect["mask_tokens"] if t in sent], "leaked": leaked})

    return {"id": case["id"], "category": case["category"], "utterance": case["utterance"],
            "pass": all(c["pass"] for c in checks.values()), "checks": checks, "actual": actual,
            "node_stdout": out.get("stdout", "")}


def _last_user_message(merged: Dict[str, Any]) -> Optional[str]:
    for msg in reversed(merged.get("dialog_history") or []):
        if msg.get("role") == "user":
            return msg.get("content")
    return None


async def run_case(mod, case: Dict[str, Any], sem: asyncio.Semaphore, verbose: bool) -> Dict[str, Any]:
    state = build_state(case, mod)
    async with sem:
        started = time.perf_counter()
        result = await mod.intent_recognition_node(state)
        elapsed = time.perf_counter() - started
    out = {"state": state, "result": result, "stdout": ""}
    graded = evaluate_case(mod, case, out)
    graded["latency_ms"] = round(elapsed * 1000, 1)
    return graded


def _metrics(graded: List[Dict[str, Any]]) -> Dict[str, Any]:
    """按维度聚合得分：意图准确率 / 槽位 F1 / 追问触发正确率 / 护栏拦截率 / 用例通过率。"""
    total = len(graded)
    passed = sum(1 for g in graded if g["pass"])
    summary: Dict[str, Any] = {"cases": total, "case_pass": passed, "case_pass_rate": _ratio(passed, total)}

    intent = [g["checks"]["slot:intent"] for g in graded if "slot:intent" in g["checks"]]
    summary["intent_accuracy"] = _ratio(sum(1 for c in intent if c["pass"]), len(intent))
    summary["intent_asserted_cases"] = len(intent)

    tp = fp = fn = 0
    per_slot: Dict[str, Dict[str, int]] = {s: {"tp": 0, "fp": 0, "fn": 0} for s in SLOT_KEYS}
    hallucination_checked = hallucination_ok = 0
    for g in graded:
        for slot in SLOT_KEYS:
            chk = g["checks"].get(f"slot:{slot}")
            if not chk:
                continue
            bucket = per_slot[slot]
            actual_value, expected_value = g["actual"][slot], chk["expected"]
            if expected_value is None:  # 断言"此处不该有值"：计入反幻觉，不进入 F1
                hallucination_checked += 1
                hallucination_ok += int(chk["pass"])
                if actual_value is not None:
                    bucket["fp"] += 1
                    fp += 1
                continue
            if chk["pass"]:
                bucket["tp"] += 1
                tp += 1
                continue
            bucket["fn"] += 1
            fn += 1
            if actual_value is not None:  # 抽出了错误值：既是漏检也是误检
                bucket["fp"] += 1
                fp += 1
    summary["slot_f1"] = _f1(tp, fp, fn)
    summary["slot_micro"] = {"tp": tp, "fp": fp, "fn": fn}
    summary["slot_asserted_slots"] = tp + fp + fn
    summary["per_slot_f1"] = {s: _f1(v["tp"], v["fp"], v["fn"]) for s, v in per_slot.items()}
    summary["no_hallucination_rate"] = _ratio(hallucination_ok, hallucination_checked)
    summary["hallucination_asserted_cases"] = hallucination_checked

    ask = [g["checks"]["ask_user"] for g in graded if "ask_user" in g["checks"]]
    summary["ask_trigger_accuracy"] = _ratio(sum(1 for c in ask if c["pass"]), len(ask))
    summary["ask_asserted_cases"] = len(ask)

    guard = [g["checks"]["guardrail"] for g in graded if "guardrail" in g["checks"]]
    block_exp = [c for c in guard if c["expected"] == "BLOCK"]
    non_block = [c for c in guard if c["expected"] != "BLOCK"]
    summary["guardrail_accuracy"] = _ratio(sum(1 for c in guard if c["pass"]), len(guard))
    summary["guardrail_asserted_cases"] = len(guard)
    summary["guardrail_block_recall"] = _ratio(sum(1 for c in block_exp if c["pass"]), len(block_exp))
    summary["guardrail_block_cases"] = len(block_exp)
    summary["guardrail_false_block_rate"] = _ratio(sum(1 for c in non_block if c["actual"] == "BLOCK"), len(non_block))
    summary["guardrail_non_block_cases"] = len(non_block)
    mask_exp = [c for c in guard if c["expected"] == "MASK"]
    summary["guardrail_mask_recall"] = _ratio(sum(1 for c in mask_exp if c["pass"]), len(mask_exp))
    summary["guardrail_mask_cases"] = len(mask_exp)

    pii = [g["checks"]["pii_masked"] for g in graded if "pii_masked" in g["checks"]]
    summary["pii_leak_free_rate"] = _ratio(sum(1 for c in pii if c["pass"]), len(pii))
    summary["pii_asserted_cases"] = len(pii)

    dec = [g["checks"]["decision"] for g in graded if "decision" in g["checks"]]
    summary["decision_accuracy"] = _ratio(sum(1 for c in dec if c["pass"]), len(dec))
    summary["decision_asserted_cases"] = len(dec)

    miss = [g["checks"]["missing_slots"] for g in graded if "missing_slots" in g["checks"]]
    summary["missing_slots_accuracy"] = _ratio(sum(1 for c in miss if c["pass"]), len(miss))
    summary["missing_asserted_cases"] = len(miss)

    summary["confidence_stats"] = _confidence_stats(graded)
    cmin = [g["checks"]["confidence_min"] for g in graded if "confidence_min" in g["checks"]]
    summary["confidence_min_accuracy"] = _ratio(sum(1 for c in cmin if c["pass"]), len(cmin))
    summary["confidence_asserted_cases"] = len(cmin)
    bands = [g["checks"]["band"] for g in graded if "band" in g["checks"]]
    summary["band_accuracy"] = _ratio(sum(1 for c in bands if c["pass"]), len(bands))
    summary["band_asserted_cases"] = len(bands)
    summary["band_distribution"] = dict(sorted(_band_distribution(graded).items()))

    fa = [g["checks"]["flow_action"] for g in graded if "flow_action" in g["checks"]]
    summary["flow_action_accuracy"] = _ratio(sum(1 for c in fa if c["pass"]), len(fa))
    summary["flow_action_asserted_cases"] = len(fa)
    # 挂起栈三类断言合并打分：深度与内容必须同时对，"信息还留着"才是真的（§3.2）
    stack = [c for g in graded for key in ("pending_count", "pending_intents", "slot_rounds")
             for c in [g["checks"].get(key)] if c is not None]
    summary["pending_stack_accuracy"] = _ratio(sum(1 for c in stack if c["pass"]), len(stack))
    summary["pending_stack_asserted_cases"] = len(stack)

    serr = [g["checks"]["slot_error"] for g in graded if "slot_error" in g["checks"]]
    summary["slot_error_message_accuracy"] = _ratio(sum(1 for c in serr if c["pass"]), len(serr))
    summary["slot_error_asserted_cases"] = len(serr)
    summary["llm_ms_p50"] = _pct([g["latency_ms"] for g in graded], 0.5)
    summary["llm_ms_p95"] = _pct([g["latency_ms"] for g in graded], 0.95)
    return summary


def summarize(graded: List[Dict[str, Any]]) -> Dict[str, Any]:
    summary = _metrics(graded)
    by_cat: Dict[str, Dict[str, Any]] = {}
    for cat in sorted({g["category"] for g in graded}):
        subset = [g for g in graded if g["category"] == cat]
        sub = _metrics(subset)
        sub["cases"] = len(subset)
        by_cat[cat] = sub
    summary["by_category"] = by_cat
    return summary


def _ratio(num: int, den: int) -> Optional[float]:
    return round(num / den, 4) if den else None


def _f1(tp: int, fp: int, fn: int) -> Optional[float]:
    precision = tp / (tp + fp) if (tp + fp) else 0.0
    recall = tp / (tp + fn) if (tp + fn) else 0.0
    if precision + recall == 0:
        return None
    return round(2 * precision * recall / (precision + recall), 4)


def _pct(values: List[float], q: float) -> Optional[float]:
    if not values:
        return None
    ordered = sorted(values)
    idx = min(len(ordered) - 1, int(round(q * (len(ordered) - 1))))
    return ordered[idx]


def _band_distribution(graded: List[Dict[str, Any]]) -> Dict[str, int]:
    """本轮阈值策略落段分布（§3.1）：段名 -> 用例数。"""
    dist: Dict[str, int] = {}
    for g in graded:
        band = g["actual"].get("band") or "unknown"
        dist[band] = dist.get(band, 0) + 1
    return dist


def _confidence_stats(graded: List[Dict[str, Any]]) -> Dict[str, Any]:
    """自评分分布按"意图判对/判错"分组——标定 high/low 阈值的唯一依据（§3.1 风险项）。

    intent_correct 取 slot:intent 断言结果；未断言意图的用例不参与分组，
    但仍在 overall 里给出分布，便于发现"缺槽用例自评偏高/偏低"的结构性问题。
    """
    scored, wrong, all_values = [], [], []
    for g in graded:
        conf = g["actual"].get("confidence")
        if conf is None:
            continue
        all_values.append(conf)
        chk = g["checks"].get("slot:intent")
        if chk is None:
            continue
        (scored if chk["pass"] else wrong).append(conf)

    def block(values: List[float]) -> Dict[str, Any]:
        if not values:
            return {"n": 0, "mean": None, "p10": None, "p50": None, "p90": None,
                    "below_low": 0, "mid_band": 0, "above_high": 0}
        return {
            "n": len(values),
            "mean": round(sum(values) / len(values), 4),
            "p10": _pct(values, 0.10), "p50": _pct(values, 0.50), "p90": _pct(values, 0.90),
            "below_low": sum(1 for v in values if v < CONFIDENCE_LOW),
            "mid_band": sum(1 for v in values if CONFIDENCE_LOW <= v < CONFIDENCE_HIGH),
            "above_high": sum(1 for v in values if v >= CONFIDENCE_HIGH),
        }

    return {"overall": block(all_values), "intent_correct": block(scored), "intent_wrong": block(wrong),
            "unjudged": len(all_values) - len(scored) - len(wrong)}


METRIC_ROWS = [
    ("case_pass_rate", "用例通过率", "cases"),
    ("intent_accuracy", "意图准确率", "intent_asserted_cases"),
    ("slot_f1", "槽位 micro-F1", "slot_asserted_slots"),
    ("ask_trigger_accuracy", "追问触发正确率", "ask_asserted_cases"),
    ("guardrail_accuracy", "护栏判定准确率", "guardrail_asserted_cases"),
    ("guardrail_block_recall", "注入拦截率", "guardrail_block_cases"),
    ("guardrail_mask_recall", "PII 脱敏触发率", "guardrail_mask_cases"),
    ("guardrail_false_block_rate", "误拦截率(越低越好)", "guardrail_non_block_cases"),
    ("pii_leak_free_rate", "PII 脱净率", "pii_asserted_cases"),
    ("no_hallucination_rate", "反幻觉率", "hallucination_asserted_cases"),
    ("missing_slots_accuracy", "缺失清单准确率", "missing_asserted_cases"),
    ("decision_accuracy", "决策出口准确率", "decision_asserted_cases"),
    ("slot_error_message_accuracy", "校验话术准确率", "slot_error_asserted_cases"),
    ("confidence_min_accuracy", "高置信断言达标率", "confidence_asserted_cases"),
    ("band_accuracy", "置信度落段准确率", "band_asserted_cases"),
    ("flow_action_accuracy", "会话流程关系准确率", "flow_action_asserted_cases"),
    ("pending_stack_accuracy", "挂起栈还原准确率", "pending_stack_asserted_cases"),
]


def fmt(value: Any) -> str:
    if value is None:
        return "  n/a "
    if isinstance(value, float):
        return f"{value * 100:5.1f}%"
    return str(value)


def print_report(graded: List[Dict[str, Any]], summary: Dict[str, Any], meta: Dict[str, Any], baseline: Optional[Dict[str, Any]]) -> None:
    print("=" * 78)
    print(">>> 意图与槽位抽取回归评测 (eval §2.1)")
    print(f"    路径={meta['mode']}  订单校验={meta['db']}  模型={meta['model']}  用例={meta['cases']}  用时={meta['elapsed_s']}s")
    print(f"    置信度阈值={meta['confidence_low']}~{meta['confidence_high']}  拦截={meta.get('confidence_gate', 'on')}")
    print("=" * 78)
    head = f"{'指标':<22}{'得分':>9}{'样本':>7}"
    if baseline:
        head += f"{'基线':>9}{'Δ':>9}"
    print(head)
    print("-" * len(head))
    base_sum = baseline["summary"] if baseline else {}
    for key, label, nkey in METRIC_ROWS:
        sample = summary.get(nkey) if nkey else summary.get("cases")
        row = f"{label:<22}{fmt(summary.get(key)):>9}{str(sample):>7}"
        if baseline:
            row += f"{fmt(base_sum.get(key)):>9}{_delta(summary.get(key), base_sum.get(key))}"
        print(row)
    print("-" * len(head))
    print(f"{'槽位F1: intent':<22}{fmt(summary['per_slot_f1']['intent']):>9}")
    print(f"{'槽位F1: order_id':<22}{fmt(summary['per_slot_f1']['order_id']):>9}")
    print(f"{'槽位F1: reason_type':<22}{fmt(summary['per_slot_f1']['reason_type']):>9}")
    print(f"{'节点耗时 P50/P95 (ms)':<22}{fmt_ms(summary['llm_ms_p50'])} / {fmt_ms(summary['llm_ms_p95'])}")

    print_confidence(summary, meta)

    print("\n分层得分（用例通过率）")
    print(f"{'分层':<18}{'用例':>5}{'通过':>6}{'通过率':>9}{'意图准确率':>11}{'槽位F1':>9}{'追问':>8}")
    print("-" * 66)
    for cat, sub in summary["by_category"].items():
        print(f"{cat:<18}{sub['cases']:>5}{sub['case_pass']:>6}{fmt(sub['case_pass_rate']):>9}"
              f"{fmt(sub['intent_accuracy']):>11}{fmt(sub['slot_f1']):>9}{fmt(sub['ask_trigger_accuracy']):>8}")

    failures = [g for g in graded if not g["pass"]]
    if failures:
        print(f"\n失败用例明细（{len(failures)} 条）")
        for g in failures:
            bad = {k: v for k, v in g["checks"].items() if not v["pass"]}
            detail = "; ".join(f"{k}: 期望={v['expected']} 实际={v['actual']}" for k, v in bad.items())
            print(f"  [{g['category']}] {g['id']} {g['utterance'][:38]}")
            print(f"      {detail}")

    if baseline:
        print_baseline_diff(graded, summary, baseline, meta)


def fmt_ms(value: Optional[float]) -> str:
    return "  n/a " if value is None else f"{value:.0f}"


def print_confidence(summary: Dict[str, Any], meta: Dict[str, Any]) -> None:
    """置信度面板（§3.1）：自评分按"意图判对/判错"分组 + 阈值策略落段分布。"""
    stats = summary.get("confidence_stats") or {}
    overall = stats.get("overall") or {}
    print(f"\n置信度自评（阈值 low={meta['confidence_low']} / high={meta['confidence_high']}，"
          f"拦截={meta.get('confidence_gate', 'on')}）")
    if not overall.get("n"):
        print("  (本轮没有任何用例产出 confidence，请确认模型是否在 tool_calls 中返回自评分)")
        return
    head = f"{'分组':<16}{'样本':>5}{'均值':>8}{'P10':>7}{'P50':>7}{'P90':>7}{'<low':>6}{'mid':>6}{'>=high':>8}"
    print(head)
    print("-" * len(head))
    for key, label in (("intent_correct", "意图判对"), ("intent_wrong", "意图判错"), ("overall", "全部自评分")):
        b = stats.get(key) or {}
        if not b.get("n"):
            continue
        print(f"{label:<16}{b['n']:>5}{b['mean']:>8.3f}{_num(b['p10']):>7}{_num(b['p50']):>7}{_num(b['p90']):>7}"
              f"{b['below_low']:>6}{b['mid_band']:>6}{b['above_high']:>8}")
    if stats.get("unjudged"):
        print(f"  另有 {stats['unjudged']} 条未断言意图的用例参与『全部自评分』统计")
    dist = summary.get("band_distribution") or {}
    print("  落段分布: " + "、".join(f"{k}={v}" for k, v in dist.items()))


def _num(value: Optional[float]) -> str:
    return "    n/a" if value is None else f"{value:.3f}"


def recommend_thresholds(stats: Dict[str, Any]) -> Dict[str, Any]:
    """按标注结果给出阈值建议：扫描候选切点，取"误判放行最少"的 high 与"正确放行最多"的 low。"""
    correct = [c["actual"]["confidence"] for c in stats["cases"]
               if c["actual"].get("confidence") is not None and c["checks"].get("slot:intent") and c["checks"]["slot:intent"]["pass"]]
    wrong = [c["actual"]["confidence"] for c in stats["cases"]
             if c["actual"].get("confidence") is not None and c["checks"].get("slot:intent") and not c["checks"]["slot:intent"]["pass"]]
    candidates = [round(v / 100, 2) for v in range(50, 101, 5)]
    best_high = min(candidates, key=lambda t: (sum(1 for v in wrong if v >= t), -sum(1 for v in correct if v >= t)))
    best_low = max(candidates, key=lambda t: (sum(1 for v in correct if v >= t), -sum(1 for v in wrong if v >= t)))
    return {
        "intent_correct_cases": len(correct), "intent_wrong_cases": len(wrong),
        "suggested_high": best_high,
        "suggested_high_wrong_released": sum(1 for v in wrong if v >= best_high),
        "suggested_high_correct_kept": sum(1 for v in correct if v >= best_high),
        "suggested_low": best_low,
        "suggested_low_correct_kept": sum(1 for v in correct if v >= best_low),
        "suggested_low_wrong_escalated": sum(1 for v in wrong if v < best_low),
        "sweep": [{"high": t,
                   "wrong_released": sum(1 for v in wrong if v >= t),
                   "correct_released": sum(1 for v in correct if v >= t)} for t in candidates],
    }


def write_confidence_calibration(stats: Dict[str, Any], path: Path) -> None:
    """把逐条自评分写成 markdown 标定表，供 §3.1 阈值评审留档。"""
    rows = [c for c in stats["cases"] if c["actual"].get("confidence") is not None]
    rec = recommend_thresholds(stats)
    lines = [f"# 置信度标定明细（{stats['meta']['mode']} 路径，阈值 {stats['meta']['confidence_low']}~{stats['meta']['confidence_high']}）", "",
             f"- 用例 {len(stats['cases'])} 条，其中 {len(rows)} 条产出自评分；意图断言判对 {rec['intent_correct_cases']} 条、判错 {rec['intent_wrong_cases']} 条",
             f"- 建议高阈值 {rec['suggested_high']}：该切点放行 {rec['suggested_high_correct_kept']}/{rec['intent_correct_cases']} 条正确判定，"
             f"误判放行 {rec['suggested_high_wrong_released']}/{rec['intent_wrong_cases']} 条",
             f"- 建议低阈值 {rec['suggested_low']}：低于该值转人工可拦住 {rec['suggested_low_wrong_escalated']}/{rec['intent_wrong_cases']} 条误判，"
             f"代价是同时转走 {rec['intent_correct_cases'] - rec['suggested_low_correct_kept']} 条正确判定", ""]
    if rec["intent_wrong_cases"] < 5:
        lines += [f"> 误判样本仅 {rec['intent_wrong_cases']} 条，统计上不足以支撑把高阈值下移；"
                  "维持配置默认阈值，待评测集扩充错例后复标。", ""]
    lines += ["| 高阈值 | 误判放行 | 正确直放 |", "|---|---|---|"]
    for s in rec["sweep"]:
        mark = " ←当前" if abs(s["high"] - stats["meta"]["confidence_high"]) < 1e-9 else ""
        lines.append(f"| {s['high']:.2f} | {s['wrong_released']} | {s['correct_released']}{mark} |")
    lines += ["", "| 用例 | 分层 | 自评分 | 落段 | 意图断言 | 期望 | 实际 | 话术 |",
              "|---|---|---|---|---|---|---|---|"]
    for c in sorted(rows, key=lambda x: x["actual"]["confidence"]):
        chk = c["checks"].get("slot:intent") or {}
        lines.append(f"| {c['id']} | {c['category']} | {c['actual']['confidence']:.2f} | {c['actual'].get('band')} "
                     f"| {'✅' if chk.get('pass') else '❌' if chk else '—'} | {chk.get('expected', '—')} "
                     f"| {c['actual'].get('intent')} | {c['utterance'][:30]} |")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"\n[标定] 逐条自评分已写入 {path}")


def _delta(cur: Any, base: Any) -> str:
    if not isinstance(cur, (int, float)) or not isinstance(base, (int, float)):
        return "     n/a"
    d = (cur - base) * 100
    arrow = "▲" if d > 0.05 else ("▼" if d < -0.05 else "=")
    return f"{d:+7.1f}pp{arrow}"


def print_baseline_diff(graded: List[Dict[str, Any]], summary: Dict[str, Any], baseline: Dict[str, Any], meta: Dict[str, Any]) -> None:
    print("\n与基线对比")
    base_meta = baseline.get("meta", {})
    mismatched = [f"{key}={base_meta.get(key)}（本次 {meta.get(key)}）"
                  for key in ("mode", "db", "confidence_low", "confidence_high")
                  if base_meta.get(key) and base_meta.get(key) != meta.get(key)]
    if mismatched:
        print("  [!] 基线口径与本次运行不一致：" + "、".join(mismatched) +
              "。跨路径/跨数据源/跨阈值的百分比不可直接互比，请改用同口径基线或重新 --record-baseline。")
    base_by_id = {c["id"]: c for c in baseline.get("cases", [])}
    cur_by_id = {g["id"]: g for g in graded}
    regressed = [i for i, b in base_by_id.items() if b["pass"] and i in cur_by_id and not cur_by_id[i]["pass"]]
    improved = [i for i, b in base_by_id.items() if not b["pass"] and i in cur_by_id and cur_by_id[i]["pass"]]
    base_cat = baseline["summary"].get("by_category", {})
    print(f"{'分层':<18}{'基线通过率':>12}{'本次':>10}{'Δ':>12}")
    print("-" * 52)
    for cat, sub in summary["by_category"].items():
        b_sub = base_cat.get(cat)
        b = b_sub.get("case_pass_rate") if b_sub else None
        if b_sub is None or b_sub.get("cases") != sub["cases"]:
            # 样本数不同（--ids/--limit/--category 子集运行）时的分层百分比没有可比性
            print(f"{cat:<18}{fmt(b):>12}{fmt(sub['case_pass_rate']):>10}{'样本不同,跳过':>12}")
            continue
        print(f"{cat:<18}{fmt(b):>12}{fmt(sub['case_pass_rate']):>10}{_delta(sub['case_pass_rate'], b):>12}")
    print(f"\n  退步用例 {len(regressed)} 条: {', '.join(sorted(regressed)) or '无'}")
    print(f"  进步用例 {len(improved)} 条: {', '.join(sorted(improved)) or '无'}")


async def main_async() -> int:
    parser = argparse.ArgumentParser(description="意图与槽位抽取回归评测（§2.1）")
    parser.add_argument("--dataset", type=Path, default=DEFAULT_DATASET, help="标注数据集 jsonl 路径")
    parser.add_argument("--mode", choices=["auto", "llm", "fallback"], default="auto",
                        help="auto=有 LLM_API_KEY 走真实模型，否则评测关键词降级下限基线")
    parser.add_argument("--db", choices=["auto", "live", "stub"], default="auto",
                        help="订单业务校验数据源：live=真实 PG，stub=eval/datasets/order_fixture.json（无 PG 环境可复现）")
    parser.add_argument("--category", action="append", default=[], help="只跑指定分层，可重复")
    parser.add_argument("--ids", default="", help="只跑指定用例 id，逗号分隔")
    parser.add_argument("--limit", type=int, default=0, help="最多跑前 N 条（抽样快跑）")
    parser.add_argument("--concurrency", type=int, default=0, help="并发度（默认 llm=5 / fallback=1）")
    parser.add_argument("--baseline", type=Path, default=None, help="与已记录的基线报告对比")
    parser.add_argument("--record-baseline", type=Path, default=None, help="把本次结果写为基线报告")
    parser.add_argument("--json", type=Path, default=None, help="输出完整评测报告 JSON")
    parser.add_argument("--fail-under", type=float, default=None, help="用例通过率低于该阈值(0~1)则非零退出")
    parser.add_argument("--fail-on-regression", action="store_true", help="相对基线出现退步则非零退出")
    parser.add_argument("--confidence-high", type=float, default=None,
                        help="临时覆盖意图高置信阈值（§3.1 标定用），不改 .env 也不影响被测模块默认配置")
    parser.add_argument("--confidence-low", type=float, default=None, help="临时覆盖意图低置信阈值")
    parser.add_argument("--no-confidence-gate", action="store_true",
                        help="关闭阈值拦截只记录自评分：标定跑法（先量分布，再定阈值）")
    parser.add_argument("--confidence-report", type=Path, default=None,
                        help="把逐条自评分与阈值建议写成 markdown 标定表")
    parser.add_argument("--flow-threshold", type=float, default=None,
                        help="临时覆盖换意图证据门槛（§3.2 标定用），只改本进程内的模块常量")
    parser.add_argument("--no-flow-gate", action="store_true",
                        help="关闭中途换意图与流程挂起：回归跑法，用于证明 §3.2 未改变既有分数")
    parser.add_argument("--verbose", action="store_true", help="透传节点内部日志")
    args = parser.parse_args()

    try:  # Windows 控制台默认 GBK，评测报告含中文与 ▲▼ 符号
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    if not args.verbose:  # 评测只关心得分，节点与护栏的运行日志压掉以免刷屏
        logging.disable(logging.CRITICAL)
    cases = load_cases(args.dataset)
    wanted_cats = {c.strip() for item in args.category for c in item.split(",") if c.strip()}
    if wanted_cats:
        known = {c["category"] for c in cases}
        unknown = wanted_cats - known
        if unknown:
            raise SystemExit(f"未知分层 {sorted(unknown)}，数据集内可用分层: {sorted(known)}")
        cases = [c for c in cases if c["category"] in wanted_cats]
    if args.ids:
        wanted = {i.strip() for i in args.ids.split(",") if i.strip()}
        cases = [c for c in cases if c["id"] in wanted]
    if args.limit:
        cases = cases[: args.limit]
    if not cases:
        raise SystemExit("没有匹配的用例，请检查 --category/--ids")

    mod = _load_module()
    from react_ai.config import settings

    global CONFIDENCE_LOW, CONFIDENCE_HIGH
    if args.confidence_low is not None and not 0 <= args.confidence_low <= 1:
        raise SystemExit("--confidence-low 须在 0~1 之间")
    if args.confidence_high is not None and not 0 <= args.confidence_high <= 1:
        raise SystemExit("--confidence-high 须在 0~1 之间")
    if args.confidence_low is not None and args.confidence_high is not None and args.confidence_low > args.confidence_high:
        raise SystemExit("--confidence-low 不得大于 --confidence-high")
    CONFIDENCE_LOW = args.confidence_low if args.confidence_low is not None else mod.CONFIDENCE_LOW
    CONFIDENCE_HIGH = args.confidence_high if args.confidence_high is not None else mod.CONFIDENCE_HIGH
    if args.confidence_low is not None or args.confidence_high is not None:  # 标定临时阈值：只改本进程内的模块常量
        mod.CONFIDENCE_LOW = CONFIDENCE_LOW
        mod.CONFIDENCE_HIGH = CONFIDENCE_HIGH
    if args.no_confidence_gate:                          # 标定跑法：只量自评分分布，不改变任何放行决策
        mod.CONFIDENCE_ENABLED = False
    if args.flow_threshold is not None and not 0 <= args.flow_threshold <= 1:
        raise SystemExit("--flow-threshold 须在 0~1 之间")
    if args.flow_threshold is not None:                   # §3.2 切换证据门槛标定：临时改写本进程常量，不动 .env
        mod.MIN_SWITCH_CONFIDENCE = args.flow_threshold
    if args.no_flow_gate:                                 # 回归跑法：整节关闭，分数应与 §3.2 改造前逐条一致
        mod.FLOW_ENABLED = False

    if not args.verbose:  # 节点轨迹已改为结构化日志（stderr），静默只需抬高 logger 级别
        logging.getLogger("react-ai").setLevel(logging.WARNING)
    mode = args.mode
    if mode == "auto":
        mode = "llm" if (settings.llm.api_key or os.getenv("LLM_API_KEY")) else "fallback"

    if mode == "fallback":  # 强制降级：让抽槽返回 None，节点自动走关键词规则兜底
        async def _no_llm(messages: list, thread_id: str = "", flow_context: str = "") -> None:
            return None

        mod.extract_slots_via_llm = _no_llm

    db = args.db
    if db == "auto":
        db = "live" if probe_db(mod) else "stub"
    if db == "stub":
        mod.validate_order_slot = make_stub_validate(mod, json.loads(DEFAULT_FIXTURE.read_text(encoding="utf-8")))
        print("[评测] 订单校验使用固定桩数据 order_fixture.json（不依赖 PG，结果可复现）")
    else:
        print("[评测] 订单校验走真实 PG（query_order 工具）")

    concurrency = args.concurrency or (5 if mode == "llm" else 1)
    sem = asyncio.Semaphore(concurrency)
    started = time.perf_counter()
    graded = await asyncio.gather(*(run_case(mod, c, sem, args.verbose) for c in cases))
    graded = list(graded)
    elapsed = round(time.perf_counter() - started, 1)

    summary = summarize(graded)
    meta = {
        "mode": mode,
        "db": db,
        "model": settings.llm.model_name if mode == "llm" else "关键词降级",
        "cases": len(graded),
        "elapsed_s": elapsed,
        "concurrency": concurrency,
        "dataset": str(args.dataset),
        "confidence_low": CONFIDENCE_LOW,
        "confidence_high": CONFIDENCE_HIGH,
        "confidence_gate": "off" if args.no_confidence_gate else "on",
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    baseline = json.loads(args.baseline.read_text(encoding="utf-8")) if args.baseline else None
    print_report(graded, summary, meta, baseline)

    report = {"meta": meta, "summary": summary, "cases": graded}
    if args.confidence_report:
        write_confidence_calibration({"meta": meta, "summary": summary, "cases": graded}, args.confidence_report)
    if args.json:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\n[评测] 完整报告已写入 {args.json}")
    if args.record_baseline:
        args.record_baseline.parent.mkdir(parents=True, exist_ok=True)
        args.record_baseline.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"[评测] 基线已记录到 {args.record_baseline}（mode={mode}, db={db}）")

    if baseline:
        base_pass = baseline["summary"].get("case_pass_rate")
        if args.fail_on_regression and base_pass is not None and summary["case_pass_rate"] < base_pass:
            print(f"[门禁] 用例通过率低于基线：{summary['case_pass_rate']:.4f} < {base_pass:.4f}")
            return 1
    if args.fail_under is not None and summary["case_pass_rate"] < args.fail_under:
        print(f"[门禁] 用例通过率 {summary['case_pass_rate']:.4f} 低于阈值 {args.fail_under}")
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main_async()))
