"""中途换意图与流程挂起测试（第二轮方案 §3.2）。

零外部依赖：LLM 用桩路由器（可注入 flow_action 判定）、订单校验用桩函数。
覆盖挂起栈的入栈/出栈/挤出、换意图的槽位继承与缓存作废、恢复的轮次还原、
放弃与越界的对称处理、会话级追问预算、终态流程不可再挂起，以及灰度开关关闭后的旧行为。
"""
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[2]
for _path in (str(ROOT / "src"), str(ROOT / "examples")):
    if _path not in sys.path:
        sys.path.insert(0, _path)

import aftersales_langgraph as demo  # noqa: E402  被测链路


class _FlowRouter:
    """LLM 网关桩：按预设槽位返回 tool_calls，用于注入不同的 flow_action 判定。"""

    def __init__(self, slots):
        self.slots = dict(slots)
        self.contexts = []                                 # 每次调用收到的会话状态段，用于验证 §3.2 是否下发给模型

    async def generate(self, messages, **kwargs):
        self.contexts.append(messages[0].content if messages else "")
        return SimpleNamespace(tool_calls=[SimpleNamespace(arguments=dict(self.slots))], content="")


def _intent_slot(value, confidence=0.9):
    return {"value": value, "confidence": confidence, "source": "LLM"}


def _order_slot(value, confidence=0.9):
    return {"string_value": value, "confidence": confidence, "source": "LLM"}


def _snapshot(intent, order=None, missing=("reason_type",), rounds=0, validated=None):
    """构造一条挂起快照：字段与 _flow_snapshot 的产出契约一致。"""
    slots = {"intent": _intent_slot(intent)}
    if order:
        slots["order_id"] = _order_slot(order)
    return {
        "flow_id": f"{intent}:{order or 'NO_ORDER'}",
        "intent": intent,
        "slot_values": slots,
        "missing_slots": list(missing),
        "slot_rounds": rounds,
        "order_validated_id": validated or order,
        "order_info": {"order_id": order} if order else None,
        "parked_at_turn": 1,
    }


class FlowSwitchTestBase(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        demo.metrics.reset()
        self._orig_router = demo.get_llm_router
        self._orig_validate = demo.validate_order_slot
        self._orig = {n: getattr(demo, n) for n in
                      ("CONFIDENCE_ENABLED", "FLOW_ENABLED", "MIN_SWITCH_CONFIDENCE",
                       "MAX_PENDING_FLOWS", "MAX_SESSION_SLOT_ROUNDS")}
        demo.CONFIDENCE_ENABLED = False        # 本文件只测流程挂起，置信网关由 test_intent_confidence 负责
        demo.FLOW_ENABLED = True
        demo.MIN_SWITCH_CONFIDENCE = 0.7
        demo.MAX_PENDING_FLOWS = 2
        demo.MAX_SESSION_SLOT_ROUNDS = 6
        demo.validate_order_slot = lambda order_id, user_id: (None, {"order_id": order_id, "user_id": user_id,
                                                                     "total_amount": 199.0,
                                                                     "items": [{"category": "CLOTHING"}]})

    def tearDown(self):
        demo.get_llm_router = self._orig_router
        demo.validate_order_slot = self._orig_validate
        for name, value in self._orig.items():
            setattr(demo, name, value)

    def use_llm(self, slots):
        router = _FlowRouter(slots)
        demo.get_llm_router = lambda: router
        return router

    def state(self, message: str, **extra) -> dict:
        base = {"session_id": "SESS_FLOW_001", "user_id": "USR_001",
                "user_message": message, "thread_id": "default:USR_001:SESS_FLOW_001",
                "slot_values": {}, "pending_flows": [], "slot_rounds": 0, "flow_turn_total": 0}
        base.update(extra)
        return base

    async def run_intent(self, state: dict) -> dict:
        return await demo.intent_recognition_node(state)


class TestSwitchParking(FlowSwitchTestBase):
    async def test_new_intent_parks_active_flow(self):
        state = self.state("还是帮我退货退款吧，换个单子 ORD_CLOTH_001，鞋子开胶了",
                           slot_values={"intent": _intent_slot("EXCHANGE"),
                                        "order_id": _order_slot("ORD_CLOTH_002")},
                           missing_slots=["reason_type"])
        router = self.use_llm({"intent": "RETURN_AND_REFUND", "order_id": "ORD_CLOTH_001",
                               "reason_type": "quality_issue", "confidence": 0.95,
                               "flow_action": "switch"})
        out = await self.run_intent(state)
        self.assertIn("【当前会话状态】", router.contexts[0])   # 有进行中流程时必须把关系判定要求下发给模型
        self.assertEqual(out["flow_action"], "switch")
        self.assertEqual([p["intent"] for p in out["pending_flows"]], ["EXCHANGE"])
        parked = out["pending_flows"][0]
        self.assertEqual(parked["missing_slots"], ["reason_type"])
        self.assertEqual(parked["slot_values"]["order_id"]["string_value"], "ORD_CLOTH_002")
        self.assertEqual(out["slot_rounds"], 0, "改口后新流程重新享有完整补槽预算")
        self.assertEqual(out["order_id"], "ORD_CLOTH_001")
        self.assertEqual(demo.metrics.counter("pending_flow_total", {"op": "park"}), 1.0)

    async def test_switch_inherits_order_and_reason(self):
        self.use_llm({"intent": "EXCHANGE", "confidence": 0.95, "flow_action": "switch"})
        state = self.state("算了，还是帮我换一个吧",
                           slot_values={"intent": _intent_slot("RETURN_AND_REFUND"),
                                        "order_id": _order_slot("ORD_CLOTH_002"),
                                        "reason_type": _intent_slot("quality_issue")},
                           missing_slots=[], intent_confirm_pending="RETURN_AND_REFUND",
                           order_validated_id="ORD_CLOTH_002")
        out = await self.run_intent(state)
        self.assertEqual(out["flow_action"], "switch")
        self.assertEqual(out["intent"], "EXCHANGE")
        self.assertEqual(out["order_id"], "ORD_CLOTH_002", "换办理类型不该丢掉已核验的订单")
        self.assertEqual(out["reason_type"], "quality_issue")
        self.assertEqual(out["missing_slots"], [], "继承补齐后不应再追问")
        self.assertNotIn("order_validated_id", out, "沿用同一笔订单：校验标记不该被作废，也不必重查")
        self.assertEqual([p["intent"] for p in out["pending_flows"]], ["RETURN_AND_REFUND"])

    async def test_switch_to_new_order_invalidates_cache(self):
        self.use_llm({"intent": "RETURN_AND_REFUND", "order_id": "ORD_CLOTH_002",
                      "reason_type": "quality_issue", "confidence": 0.95, "flow_action": "switch"})
        state = self.state("还是退货退款吧，另一个单 ORD_CLOTH_002",
                           slot_values={"intent": _intent_slot("EXCHANGE"),
                                        "order_id": _order_slot("ORD_CLOTH_001")},
                           missing_slots=["reason_type"],
                           order_validated_id="ORD_CLOTH_001",
                           order_info={"order_id": "ORD_CLOTH_001"})
        out = await self.run_intent(state)
        self.assertEqual(out["order_id"], "ORD_CLOTH_002", "本轮自带的订单号必须赢过继承值")
        self.assertEqual(out["order_validated_id"], "ORD_CLOTH_002")
        self.assertEqual(out["order_info"]["order_id"], "ORD_CLOTH_002", "订单缓存换单后不得留旧值，否则串单")
        self.assertEqual(out["pending_flows"][0]["order_validated_id"], "ORD_CLOTH_001",
                         "旧流程的校验标记要跟着快照走，恢复时才能免二次查库")

    async def test_low_confidence_switch_is_rejected(self):
        self.use_llm({"intent": "RETURN_AND_REFUND", "order_id": "ORD_CLOTH_002",
                      "confidence": 0.3, "flow_action": "switch"})
        state = self.state("呃那个，ORD_CLOTH_002",
                           slot_values={"intent": _intent_slot("EXCHANGE"),
                                        "order_id": _order_slot("ORD_CLOTH_002")},
                           missing_slots=["reason_type"])
        out = await self.run_intent(state)
        self.assertEqual(out["flow_action"], "continue", "证据不足的改口按补充旧流程处理")
        self.assertEqual(out["pending_flows"], [], "被拒绝的改口不得入栈")
        self.assertEqual(out["intent"], "EXCHANGE")
        self.assertEqual(demo.metrics.counter("flow_switch_rejected_total", {"reason": "low_confidence"}), 1.0)

    async def test_switch_without_new_intent_is_rejected(self):
        self.use_llm({"confidence": 0.95, "flow_action": "switch"})
        state = self.state("嗯嗯",
                           slot_values={"intent": _intent_slot("EXCHANGE"),
                                        "order_id": _order_slot("ORD_CLOTH_002")},
                           missing_slots=["reason_type"])
        out = await self.run_intent(state)
        self.assertEqual(out["flow_action"], "continue")
        self.assertEqual(demo.metrics.counter("flow_switch_rejected_total", {"reason": "no_new_intent"}), 1.0)

    async def test_threshold_is_configurable(self):
        demo.MIN_SWITCH_CONFIDENCE = 0.2
        self.use_llm({"intent": "RETURN_AND_REFUND", "order_id": "ORD_CLOTH_002",
                      "reason_type": "quality_issue", "confidence": 0.3, "flow_action": "switch"})
        state = self.state("改成退货退款吧，ORD_CLOTH_002",
                           slot_values={"intent": _intent_slot("EXCHANGE"),
                                        "order_id": _order_slot("ORD_CLOTH_002")},
                           missing_slots=["reason_type"])
        out = await self.run_intent(state)
        self.assertEqual(out["flow_action"], "switch", "阈值下调后同样的证据应当放行")

    async def test_no_active_flow_keeps_legacy_behavior(self):
        """会话里没有进行中流程时，模型自称 switch 也无事可做——本节必须完全不改变旧行为。"""
        self.use_llm({"intent": "RETURN_AND_REFUND", "order_id": "ORD_CLOTH_001",
                      "reason_type": "quality_issue", "confidence": 0.95, "flow_action": "switch"})
        out = await self.run_intent(self.state("我要退货，订单 ORD_CLOTH_001，质量问题"))
        self.assertEqual(out["flow_action"], "continue")
        self.assertEqual(out["pending_flows"], [])
        self.assertEqual(out["intent"], "RETURN_AND_REFUND")
        self.assertEqual(out["missing_slots"], [])

    async def test_slot_answer_turn_is_not_switch(self):
        """用户只是回答补槽提问时，即使话里带"换"字也不该把旧流程埋掉——误判代价最高的一种。"""
        self.use_llm({"reason_type": "quality_issue", "confidence": 0.95})
        state = self.state("鞋子穿两次就开胶了",
                           slot_values={"intent": _intent_slot("EXCHANGE"),
                                        "order_id": _order_slot("ORD_CLOTH_002")},
                           missing_slots=["reason_type"])
        out = await self.run_intent(state)
        self.assertEqual(out["flow_action"], "continue")
        self.assertEqual(out["pending_flows"], [])
        self.assertEqual(out["missing_slots"], [])
        self.assertEqual(out["intent"], "EXCHANGE", "补槽轮不该动意图")


class TestResume(FlowSwitchTestBase):
    async def test_resume_restores_slots_and_rounds(self):
        snap = _snapshot("RETURN_AND_REFUND", "ORD_CLOTH_001", missing=("reason_type",), rounds=2)
        self.use_llm({"reason_type": "no_reason", "confidence": 0.9})
        state = self.state("继续退货吧，ORD_CLOTH_001", pending_flows=[snap],
                           slot_values={}, missing_slots=["intent", "order_id", "reason_type"])
        out = await self.run_intent(state)
        self.assertEqual(out["flow_action"], "resume")
        self.assertEqual(out["pending_flows"], [], "恢复即出栈")
        self.assertEqual(out["intent"], "RETURN_AND_REFUND")
        self.assertEqual(out["order_id"], "ORD_CLOTH_001")
        self.assertEqual(out["slot_rounds"], 2, "该流程已消耗的追问轮数要一并还原")
        self.assertEqual(out["order_validated_id"], "ORD_CLOTH_001", "恢复后免二次查库")
        self.assertEqual(demo.metrics.counter("pending_flow_total", {"op": "resume"}), 1.0)

    async def test_resume_parks_active_flow_symmetrically(self):
        """改口去恢复另一个流程同样是换意图：当前流程必须挂起，不能静默丢掉用户已填的信息。"""
        self.use_llm({"reason_type": "quality_issue", "confidence": 0.9})
        state = self.state("继续办仅退款吧，ORD_FRESH_003",
                           slot_values={"intent": _intent_slot("EXCHANGE"),
                                        "order_id": _order_slot("ORD_CLOTH_002")},
                           missing_slots=["reason_type"],
                           pending_flows=[_snapshot("REFUND_ONLY", "ORD_FRESH_003")])
        out = await self.run_intent(state)
        self.assertEqual(out["flow_action"], "resume")
        self.assertEqual(out["intent"], "REFUND_ONLY")
        self.assertEqual([p["intent"] for p in out["pending_flows"]], ["EXCHANGE"])

    async def test_named_resume_beats_stack_top(self):
        self.use_llm({"confidence": 0.9})
        state = self.state("继续仅退款，ORD_FRESH_003",
                           pending_flows=[_snapshot("EXCHANGE", "ORD_CLOTH_002"),
                                          _snapshot("REFUND_ONLY", "ORD_FRESH_003")])
        out = await self.run_intent(state)
        self.assertEqual(out["intent"], "REFUND_ONLY")
        self.assertEqual([p["intent"] for p in out["pending_flows"]], ["EXCHANGE"],
                         "点名恢复时只弹出被点名的那条")

    async def test_default_resume_takes_most_recent(self):
        self.use_llm({"confidence": 0.9})
        state = self.state("继续上一个吧",
                           pending_flows=[_snapshot("REFUND_ONLY", "ORD_FRESH_003"),
                                          _snapshot("EXCHANGE", "ORD_CLOTH_002")])
        out = await self.run_intent(state)
        self.assertEqual(out["intent"], "REFUND_ONLY", "没点名即恢复最近挂起的一条（栈语义：栈顶排最前）")
        self.assertEqual([p["intent"] for p in out["pending_flows"]], ["EXCHANGE"])

    async def test_resume_works_without_llm(self):
        """关键词网关故障时也要能恢复：§3.2 的前置正则通道不依赖模型。"""
        snap = _snapshot("RETURN_AND_REFUND", "ORD_CLOTH_001", missing=("reason_type",))

        class _Down:
            async def generate(self, messages, **kwargs):
                raise RuntimeError("模拟 LLM 网关不可用")

        demo.get_llm_router = lambda: _Down()
        state = self.state("继续退货吧，ORD_CLOTH_001", pending_flows=[snap])
        out = await self.run_intent(state)
        self.assertEqual(out["flow_action"], "resume")
        self.assertEqual(out["intent"], "RETURN_AND_REFUND")
        self.assertEqual(out["order_id"], "ORD_CLOTH_001")

    async def test_resume_turn_ignores_model_self_declared_switch(self):
        """恢复轮没向模型索取会话关系判定（flow_context 为空），模型自述的 switch 不得把刚恢复的流程再埋回栈里。"""
        snap = _snapshot("RETURN_AND_REFUND", "ORD_CLOTH_001", missing=("reason_type",), rounds=2)
        self.use_llm({"intent": "RETURN_AND_REFUND", "order_id": "ORD_CLOTH_002",
                      "reason_type": "quality_issue", "confidence": 0.9, "flow_action": "switch"})
        state = self.state("继续退货", pending_flows=[snap],
                           slot_values={}, missing_slots=["intent", "order_id", "reason_type"])
        out = await self.run_intent(state)
        self.assertEqual(out["flow_action"], "resume")
        self.assertEqual(out["order_id"], "ORD_CLOTH_001", "恢复的是快照里那笔单，不被本轮抽到的单号顶掉")
        self.assertEqual([p["intent"] for p in out["pending_flows"]], [], "刚恢复的流程不该立刻被挂回去")
        self.assertEqual(out["slot_rounds"], 2)

    async def test_resume_needs_pending_stack(self):
        self.use_llm({"intent": "RETURN_AND_REFUND", "order_id": "ORD_CLOTH_001",
                      "reason_type": "quality_issue", "confidence": 0.9})
        out = await self.run_intent(self.state("继续退货吧，订单 ORD_CLOTH_001"))
        self.assertEqual(out["flow_action"], "continue", "空栈时无事可恢复，行为等同旧版")


class TestAbortAndOffDomain(FlowSwitchTestBase):
    async def test_abort_clears_active_but_keeps_parked(self):
        self.use_llm({"flow_action": "abort", "confidence": 0.9})
        state = self.state("这个退货不办了",
                           slot_values={"intent": _intent_slot("RETURN_AND_REFUND"),
                                        "order_id": _order_slot("ORD_CLOTH_001")},
                           missing_slots=["reason_type"],
                           pending_flows=[_snapshot("EXCHANGE", "ORD_CLOTH_002")])
        out = await self.run_intent(state)
        self.assertEqual(out["decision"], "FLOW_ABORTED")
        self.assertEqual(out["slot_values"], {})
        self.assertEqual([p["intent"] for p in out["pending_flows"]], ["EXCHANGE"],
                         "放弃当前流程不该连带清空更早挂起的流程")
        self.assertIn("继续换货", out["final_reply"], "答复要告诉用户旧流程还能回来")
        self.assertEqual(demo.route_slots({**state, **out}), "send_notification", "取消轮不进任何写操作")
        self.assertEqual(demo.metrics.counter("pending_flow_total", {"op": "abort"}), 1.0)

    async def test_abort_is_not_triggered_without_active_flow(self):
        self.use_llm({"flow_action": "abort", "confidence": 0.9})
        out = await self.run_intent(self.state("这个退货不办了"))
        self.assertNotEqual(out.get("decision"), "FLOW_ABORTED")
        self.assertEqual(out["flow_action"], "continue")

    async def test_keyword_abort_needs_a_flow(self):
        """降级路径的放弃判定同样只在有进行中流程时生效。"""
        self.assertEqual(demo.fallback_extract_slots("算了，不办了")["flow_action"], None)
        self.assertEqual(demo.fallback_extract_slots("算了，不办了", "RETURN_AND_REFUND")["flow_action"], "abort")
        self.assertIsNone(demo.fallback_extract_slots("算了，不办了", "RETURN_AND_REFUND")["intent"])

    async def test_keyword_switch_requires_cue_and_different_intent(self):
        same = demo.fallback_extract_slots("还是要退货退款，ORD_CLOTH_001", "RETURN_AND_REFUND")
        self.assertEqual(same["flow_action"], "switch", "规则层放行，单号是否真不同由主节点核验")
        other = demo.fallback_extract_slots("还是换成换货吧，ORD_CLOTH_001", "RETURN_AND_REFUND")
        self.assertEqual(other["flow_action"], "switch")
        no_cue = demo.fallback_extract_slots("那就换货吧，ORD_CLOTH_001", "RETURN_AND_REFUND")
        self.assertEqual(no_cue["flow_action"], "continue", "没有改口线索词时规则不敢判切换")
        vague = demo.fallback_extract_slots("还是它吧", "RETURN_AND_REFUND")
        self.assertEqual(vague["flow_action"], "continue", "既没换诉求也没报单号，就是接着办")

    async def test_keyword_path_never_declares_off_domain(self):
        """规则无法区分『好的马上找』与『帮我查物流』，误判会埋掉已填槽位：降级路径刻意不判 off_domain。"""
        for text in ("帮我查下物流", "好的马上找", "你们的客服电话多少"):
            self.assertNotIn(demo.fallback_extract_slots(text, "RETURN_AND_REFUND")["flow_action"],
                             ("off_domain", "switch"))

    async def test_off_domain_parks_active_flow(self):
        self.use_llm({"intent": "OTHER", "flow_action": "off_domain", "confidence": 0.9})
        state = self.state("先不弄这个了，帮我查下物流",
                           slot_values={"intent": _intent_slot("RETURN_AND_REFUND"),
                                        "order_id": _order_slot("ORD_CLOTH_001")},
                           missing_slots=["reason_type"])
        out = await self.run_intent(state)
        self.assertEqual(out["decision"], "OUT_OF_SCOPE")
        self.assertEqual([p["intent"] for p in out["pending_flows"]], ["RETURN_AND_REFUND"])
        self.assertEqual(out["intent"], "OTHER")
        self.assertNotIn("order_id", out["slot_values"], "越界答复不留旧流程的订单槽位")
        self.assertIn("退货退款", out["final_reply"], "越界出口同样要给出恢复入口")


class TestStackCapAndTerminal(FlowSwitchTestBase):
    async def test_stack_cap_evicts_oldest(self):
        self.use_llm({"intent": "EXCHANGE", "order_id": "ORD_CLOTH_002",
                      "reason_type": "quality_issue", "confidence": 0.9, "flow_action": "switch"})
        state = self.state("还是换货吧，ORD_CLOTH_002",
                           slot_values={"intent": _intent_slot("REFUND_ONLY"),
                                        "order_id": _order_slot("ORD_FRESH_003")},
                           missing_slots=["reason_type"],
                           pending_flows=[_snapshot("RETURN_AND_REFUND", "ORD_CLOTH_001"),
                                          _snapshot("EXCHANGE", "ORD_CLOTH_002", rounds=1)])
        out = await self.run_intent(state)
        self.assertEqual(len(out["pending_flows"]), 2, "挂起栈深度必须封顶")
        self.assertEqual(out["pending_flows"][0]["intent"], "REFUND_ONLY")
        self.assertEqual(demo.metrics.counter("pending_flow_total", {"op": "evict"}), 1.0)

    async def test_same_intent_new_order_is_a_different_flow(self):
        """『再退另一个单子』办理类型没变但是笔新单：flow_id 含订单号，旧流程同样要挂起来。"""
        self.use_llm({"intent": "RETURN_AND_REFUND", "order_id": "ORD_CLOTH_002",
                      "reason_type": "quality_issue", "confidence": 0.9, "flow_action": "switch"})
        state = self.state("同一个理由，换个单子 ORD_CLOTH_002 也退了吧",
                           slot_values={"intent": _intent_slot("RETURN_AND_REFUND"),
                                        "order_id": _order_slot("ORD_CLOTH_001")},
                           missing_slots=["reason_type"],
                           pending_flows=[_snapshot("RETURN_AND_REFUND", "ORD_CLOTH_003")])
        out = await self.run_intent(state)
        self.assertEqual(out["flow_action"], "switch")
        self.assertEqual(len(out["pending_flows"]), 2, "同一意图换订单属于不同流程，各占一格")
        self.assertEqual({p["flow_id"] for p in out["pending_flows"]},
                         {"RETURN_AND_REFUND:ORD_CLOTH_001", "RETURN_AND_REFUND:ORD_CLOTH_003"})
        self.assertEqual(out["order_id"], "ORD_CLOTH_002", "用户新报的单号必须生效，不能被在办单号挡掉")

    async def test_same_intent_same_order_is_not_a_new_flow(self):
        """复述在办单号只是补充说明，不是另起一单：旧流程绝不能被埋掉。"""
        self.use_llm({"intent": "RETURN_AND_REFUND", "order_id": "ORD_CLOTH_001",
                      "reason_type": "quality_issue", "confidence": 0.9, "flow_action": "switch"})
        state = self.state("还是退货退款吧，订单号 ORD_CLOTH_001",
                           slot_values={"intent": _intent_slot("RETURN_AND_REFUND"),
                                        "order_id": _order_slot("ORD_CLOTH_001")},
                           missing_slots=["reason_type"])
        out = await self.run_intent(state)
        self.assertEqual(out["flow_action"], "continue")
        self.assertEqual(out["pending_flows"], [])
        self.assertEqual(demo.metrics.counter("flow_switch_rejected_total", {"reason": "no_new_intent"}), 1.0)

    async def test_repeated_switch_does_not_duplicate_snapshot(self):
        """同一流程被反复挂起时按 flow_id 去重，避免栈里出现两份相同的已填信息。"""
        self.use_llm({"intent": "RETURN_AND_REFUND", "order_id": "ORD_CLOTH_001",
                      "reason_type": "quality_issue", "confidence": 0.9, "flow_action": "switch"})
        state = self.state("先退货退款吧，ORD_CLOTH_001，质量问题",
                           slot_values={"intent": _intent_slot("EXCHANGE"),
                                        "order_id": _order_slot("ORD_CLOTH_002")},
                           missing_slots=["reason_type"],
                           pending_flows=[_snapshot("EXCHANGE", "ORD_CLOTH_002")])
        out = await self.run_intent(state)
        self.assertEqual(len(out["pending_flows"]), 1)
        self.assertEqual(out["pending_flows"][0]["flow_id"], "EXCHANGE:ORD_CLOTH_002")

    async def test_terminal_flow_is_not_parked_and_table_resets(self):
        self.use_llm({"intent": "RETURN_AND_REFUND", "order_id": "ORD_CLOTH_002",
                      "reason_type": "quality_issue", "confidence": 0.9, "flow_action": "switch"})
        state = self.state("再帮我退一个 ORD_CLOTH_002，质量问题",
                           slot_values={"intent": _intent_slot("RETURN_AND_REFUND"),
                                        "order_id": _order_slot("ORD_CLOTH_001")},
                           missing_slots=[], decision="AUTO_REFUND", flow_status="completed",
                           order_validated_id="ORD_CLOTH_001")
        out = await self.run_intent(state)
        self.assertEqual(out["pending_flows"], [], "办结过的流程不得入栈：恢复出去会二次写退款单")
        self.assertEqual(out["intent"], "RETURN_AND_REFUND")
        self.assertEqual(out["order_id"], "ORD_CLOTH_002", "已办结 ⇒ 本轮是新一次办理，槽位表应重开")

    async def test_terminal_marker_and_session_budget_reset(self):
        out = demo.send_notification_node(self.state("好的", decision="AUTO_REFUND",
                                                     order_id="ORD_CLOTH_001", flow_turn_total=5))
        self.assertEqual(out["flow_status"], "completed")
        self.assertEqual(out["flow_turn_total"], 0,
                         "预算防的是流程间反复切换刷追问，不是整个会话只能被反问 6 次")
        self.assertEqual(demo.metrics.counter("flow_terminal_total", {"status": "completed"}), 1.0)

    async def test_ask_user_session_budget_escalates(self):
        demo.MAX_SESSION_SLOT_ROUNDS = 2
        state = self.state("还要什么", missing_slots=["order_id", "reason_type"],
                           slot_rounds=1, flow_turn_total=2, decision="RETRY_ASK",
                           slot_values={"intent": _intent_slot("RETURN_AND_REFUND")})
        out = demo.ask_user_node(state)
        self.assertEqual(out["decision"], "ESCALATE_HUMAN", "会话级预算耗尽应转人工")
        self.assertIn("多流程切换", out["human_reason"])
        self.assertEqual(out["flow_turn_total"], 3)

    async def test_ask_user_flow_budget_escalates_first(self):
        state = self.state("还要什么", missing_slots=["reason_type"],
                           slot_rounds=3, flow_turn_total=1, decision="RETRY_ASK",
                           slot_values={"intent": _intent_slot("RETURN_AND_REFUND"),
                                        "order_id": _order_slot("ORD_CLOTH_001")})
        out = demo.ask_user_node(state)
        self.assertEqual(out["decision"], "ESCALATE_HUMAN")
        self.assertIn("多次追问后必要信息仍不完整", out["human_reason"], "单流程预算先于会话预算")

    async def test_ask_user_appends_resume_hint_after_switch(self):
        state = self.state("换个单子吧", missing_slots=["order_id"],
                           flow_action="switch",
                           pending_flows=[_snapshot("RETURN_AND_REFUND", "ORD_CLOTH_001")],
                           slot_values={"intent": _intent_slot("EXCHANGE")})
        out = demo.ask_user_node(state)
        self.assertIn("继续退货退款", out["final_reply"], "本轮刚挂起旧流程就必须告知退路")


class TestFeatureFlag(FlowSwitchTestBase):
    async def test_disabled_reproduces_legacy_behavior(self):
        demo.FLOW_ENABLED = False
        snap = _snapshot("EXCHANGE", "ORD_CLOTH_002")
        self.use_llm({"intent": "RETURN_AND_REFUND", "order_id": "ORD_CLOTH_001",
                      "reason_type": "quality_issue", "confidence": 0.9, "flow_action": "switch"})
        state = self.state("算了帮我退货 ORD_CLOTH_001，质量有问题",
                           slot_values={"intent": _intent_slot("EXCHANGE"),
                                        "order_id": _order_slot("ORD_CLOTH_002")},
                           missing_slots=["reason_type"], pending_flows=[snap])
        out = await self.run_intent(state)
        self.assertEqual(out["flow_action"], "continue", "开关关闭时不改写任何流程语义")
        self.assertEqual(out["pending_flows"], [snap], "开关关闭时挂起栈原样保留")
        self.assertEqual(out["intent"], "EXCHANGE", "旧版 accumulate-never-overwrite：已填意图不被覆盖")
        self.assertEqual(out["order_id"], "ORD_CLOTH_002", "旧版同样不会被新单号覆盖")
        self.assertEqual(out["missing_slots"], [], "本轮报出的原因大类照常补进旧流程")

    async def test_disabled_never_parks_on_resume_wording(self):
        demo.FLOW_ENABLED = False
        snap = _snapshot("RETURN_AND_REFUND", "ORD_CLOTH_001")
        self.use_llm({"intent": "EXCHANGE", "order_id": "ORD_CLOTH_002",
                      "reason_type": "quality_issue", "confidence": 0.9})
        state = self.state("继续退货吧，另外这个也要换货 ORD_CLOTH_002",
                           slot_values={"intent": _intent_slot("EXCHANGE"),
                                        "order_id": _order_slot("ORD_CLOTH_002")},
                           missing_slots=["reason_type"], pending_flows=[snap])
        out = await self.run_intent(state)
        self.assertEqual(out["pending_flows"], [snap], "开关关闭时不弹出、不压入")
        self.assertNotIn("slot_rounds", out, "开关关闭时不改写追问轮次")
        self.assertEqual(out["flow_action"], "continue")


if __name__ == "__main__":
    unittest.main()
