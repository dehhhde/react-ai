"""意图评测集完整性校验（第二轮方案 §2.1）.

只校验数据集自身的结构与覆盖度，不调用 LLM、不连 PG，因此可进 CI 每次 PR 常跑：
标注数据一旦被人改坏（重复 id、非法枚举、引用了桩数据里不存在的订单号），
评测分数会静默失真，这条测试把它挡在前面。
"""
import json
import unittest
from collections import Counter
from pathlib import Path

EVAL_DIR = Path(__file__).resolve().parents[2] / "eval"
DATASET = EVAL_DIR / "datasets" / "intent_slots.jsonl"
FIXTURE = EVAL_DIR / "datasets" / "order_fixture.json"

MIN_TOTAL_CASES = 100
MIN_PER_LAYER = {
    "complete": 10,
    "missing": 10,
    "multi_turn": 10,
    "colloquial": 10,
    "out_of_scope": 10,
    "injection": 8,
    "pii": 8,
    "order_validation": 4,
    "flow_switch": 12,
}
ALLOWED_EXPECT_KEYS = {
    "intent", "intent_accept", "order_id", "reason_type", "missing_slots", "missing_include",
    "ask_user", "guardrail", "decision", "slot_error_contains", "mask_tokens",
    "confidence_min", "band", "flow_action", "pending_count", "pending_intents", "slot_rounds",
}
ALLOWED_INTENT = {"RETURN_AND_REFUND", "REFUND_ONLY", "EXCHANGE", "OTHER"}
# 置信度落段枚举（§3.1），与 eval/run_intent_eval.py 的 BANDS 保持一致
ALLOWED_BAND = {"high", "mid", "mid_deferred", "low", "confirmed", "unrated",
                "not_gated", "out_of_scope", "guardrail_blocked"}
# 会话流程关系枚举（§3.2），与 eval/run_intent_eval.py 的 FLOW_ACTIONS 保持一致
ALLOWED_FLOW_ACTION = {"continue", "switch", "resume", "abort", "off_domain"}
REQUIRED_SLOTS = ("intent", "order_id", "reason_type")
# 短路到终局答复/人工的决策：这些轮次不再走补槽，缺槽断言不适用
TERMINAL_DECISIONS = {"OUT_OF_SCOPE", "FLOW_ABORTED", "ESCALATE_HUMAN"}
# 数据集内故意写的坏订单号：用于验证业务校验剔除与回流追问，不应存在于订单桩数据中
DELIBERATE_BAD_ORDERS = {"ORD_NOT_EXIST_999", "ORD_ZZZ_123", "ORD_CLOTH_999"}


def _load_cases():
    with DATASET.open(encoding="utf-8") as fh:
        return [json.loads(line) for line in fh if line.strip()]


class TestIntentEvalDataset(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.cases = _load_cases()
        cls.fixture_orders = set(json.loads(FIXTURE.read_text(encoding="utf-8"))["orders"])

    def test_layer_coverage_meets_minimum(self):
        counts = Counter(case["category"] for case in self.cases)
        self.assertGreaterEqual(len(self.cases), MIN_TOTAL_CASES, f"用例总量需 ≥{MIN_TOTAL_CASES}")
        self.assertEqual(set(counts), set(MIN_PER_LAYER), "分层集合与文档约定不一致")
        for layer, minimum in MIN_PER_LAYER.items():
            self.assertGreaterEqual(counts[layer], minimum, f"{layer} 层用例不足 {minimum} 条")

    def test_ids_are_unique(self):
        ids = [case["id"] for case in self.cases]
        duplicated = [i for i, c in Counter(ids).items() if c > 1]
        self.assertEqual(duplicated, [], f"用例 id 重复: {duplicated}")

    def test_required_fields_present(self):
        for case in self.cases:
            with self.subTest(case=case["id"]):
                self.assertIn("utterance", case)
                self.assertTrue(case["utterance"].strip(), "话术不得为空")
                self.assertIn("expect", case)
                self.assertTrue(case["expect"], "expect 至少需要一个断言项")
                unknown = set(case["expect"]) - ALLOWED_EXPECT_KEYS
                self.assertEqual(unknown, set(), f"未知断言字段 {unknown}")

    def test_enums_are_valid(self):
        for case in self.cases:
            with self.subTest(case=case["id"]):
                expect = case["expect"]
                for intent in [expect.get("intent"), *(expect.get("intent_accept") or [])]:
                    self.assertIn(intent, ALLOWED_INTENT | {None})
                for snap in case.get("seed_pending") or []:
                    self.assertIn((snap.get("slots") or {}).get("intent"), ALLOWED_INTENT - {"OTHER"},
                                  "挂起快照必须是一个真实售后意图，OTHER 不该进挂起栈")
                    for slot in snap.get("missing") or []:
                        self.assertIn(slot, REQUIRED_SLOTS, "挂起快照的缺失清单含非必要槽位")
                self.assertIn(expect.get("guardrail"), {"ALLOW", "MASK", "BLOCK", None})
                self.assertIn(expect.get("reason_type"), {"quality_issue", "no_reason", None})
                self.assertIn(expect.get("flow_action"), ALLOWED_FLOW_ACTION | {None})
                for slot in expect.get("missing_slots", []) + expect.get("missing_include", []):
                    self.assertIn(slot, REQUIRED_SLOTS)

    def test_expected_orders_exist_in_fixture(self):
        """期望能抽到的订单号必须在桩数据内，否则该用例在 --db stub 下永远不可能通过。"""
        for case in self.cases:
            expected_order = case["expect"].get("order_id")
            seeded = list((case.get("seed_slots") or {}).values())
            for snap in case.get("seed_pending") or []:
                seeded += list((snap.get("slots") or {}).values())
                if snap.get("validated"):
                    seeded.append(snap["validated"])
            with self.subTest(case=case["id"]):
                if expected_order is not None:
                    self.assertIn(expected_order, self.fixture_orders)
                for slot in seeded:
                    if isinstance(slot, str) and slot.startswith("ORD_") and slot not in DELIBERATE_BAD_ORDERS:
                        self.assertIn(slot, self.fixture_orders)

    def test_deliberate_bad_orders_stay_bad(self):
        for order in DELIBERATE_BAD_ORDERS:
            self.assertNotIn(order, self.fixture_orders)

    def test_guardrail_layers_have_block_and_mask_samples(self):
        injection = [c for c in self.cases if c["category"] == "injection"]
        pii = [c for c in self.cases if c["category"] == "pii"]
        self.assertTrue(any(c["expect"].get("guardrail") == "BLOCK" for c in injection))
        self.assertTrue(any(c["expect"].get("guardrail") == "ALLOW" for c in injection), "缺少假阳性对照用例")
        self.assertTrue(any(c["expect"].get("guardrail") == "MASK" for c in pii))
        self.assertTrue(any(c["expect"].get("guardrail") == "ALLOW" for c in pii), "缺少假阳性对照用例")

    def test_ask_user_false_implies_no_missing_required_slot(self):
        """断言"不该追问"的用例，必须同时给出三个必要槽位的期望值（护栏阻断与终局决策除外）。"""
        for case in self.cases:
            expect = case["expect"]
            if expect.get("ask_user") is not False:
                continue
            if expect.get("guardrail") == "BLOCK" or expect.get("decision") in TERMINAL_DECISIONS:
                continue
            with self.subTest(case=case["id"]):
                for slot in REQUIRED_SLOTS:
                    self.assertIsNotNone(expect.get(slot), f"{slot} 应有非空期望值，否则该用例本应触发追问")

    def test_multi_turn_cases_carry_context(self):
        for case in self.cases:
            if case["category"] != "multi_turn":
                continue
            with self.subTest(case=case["id"]):
                self.assertTrue(case.get("context"), "多轮用例必须携带对话上下文")

    def test_confidence_assertions_are_valid(self):
        """置信度断言（§3.1）：band 必须落在枚举内，confidence_min 必须是 (0,1] 的阈值。"""
        for case in self.cases:
            with self.subTest(case=case["id"]):
                expect = case["expect"]
                if "band" in expect:
                    self.assertIn(expect["band"], ALLOWED_BAND, "未知的置信度落段")
                if "confidence_min" in expect:
                    minimum = expect["confidence_min"]
                    self.assertIsInstance(minimum, (int, float))
                    self.assertGreater(minimum, 0, "confidence_min 必须大于 0，否则该断言恒真")
                    self.assertLessEqual(minimum, 1)

    def test_high_confidence_anchors_are_released_by_gate(self):
        """必须存在"表述明确→高置信直放"的正向锚点用例，否则阈值被改坏（如 high 提到 0.99）无人发现。"""
        anchors = [c for c in self.cases if c["expect"].get("band") == "high"]
        self.assertGreaterEqual(len(anchors), 4, "高置信直放锚点不足 4 条")
        for case in anchors:
            with self.subTest(case=case["id"]):
                self.assertIs(case["expect"].get("ask_user"), False,
                              "高置信锚点必须是槽位齐全、不该被追问打断的用例")
                self.assertIsNotNone(case["expect"].get("intent"), "高置信锚点必须断言意图")
        self.assertTrue(any(c["expect"].get("band") == "out_of_scope" for c in self.cases),
                        "缺少越界落段（OTHER 网关）断言")


    def test_flow_layer_covers_every_flow_action(self):
        """流程挂起层（§3.2）的组成约束：五种会话关系都要有代表，且断言之间不能自相矛盾。

        挂起栈的断言必须建立在"确有进行中/已挂起的流程"之上，否则测的是旧行为；
        slot_rounds>0 只可能来自恢复（改口会把新流程预算清零），出现在别处即标注写错。
        """
        flow = [c for c in self.cases if c["category"] == "flow_switch"]
        actions = {c["expect"].get("flow_action") for c in flow}
        self.assertTrue({"switch", "resume", "abort", "off_domain"} <= actions,
                        f"flow_switch 层缺少会话关系样本，现有 {actions}")
        for case in flow:
            expect = case["expect"]
            with self.subTest(case=case["id"]):
                if expect.get("guardrail") != "BLOCK":
                    self.assertIn("flow_action", expect, "流程层用例必须断言会话关系，否则退化成普通抽槽用例")
                intents = expect.get("pending_intents")
                if intents is not None:
                    self.assertEqual(len(intents), expect.get("pending_count"),
                                     "挂起栈内容断言必须与深度断言一致")
                if expect.get("flow_action") in ("switch", "resume", "abort", "off_domain"):
                    self.assertTrue(case.get("seed_slots") or case.get("seed_pending"),
                                    "没有进行中或已挂起的流程，无从验证换意图")
                if expect.get("slot_rounds", 0) > 0:
                    self.assertEqual(expect.get("flow_action"), "resume",
                                     "只有恢复挂起流程才还原已烧掉的追问轮数")
        self.assertTrue(any((c.get("seed_state") or {}).get("flow_status")
                            for c in flow), "缺少已办结流程不得挂起的对照用例")
        self.assertTrue(any(not (c.get("seed_slots") or c.get("seed_pending") or c.get("seed_state"))
                            for c in flow), "缺少无进行中流程的行为不变对照用例")


if __name__ == "__main__":
    unittest.main()
