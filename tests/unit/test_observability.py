"""可观测性组件单元测试（第二轮方案 §2.3）。

覆盖带标签计数器、直方图分位数、Prometheus 文本导出与结构化日志的会话关联字段；
零外部依赖（不连 PG、不调 LLM），可进 CI 常跑。
"""
import io
import json
import unittest
from contextlib import contextmanager

from react_ai.observability import MetricsCollector, StructuredLogger
from react_ai.observability.metrics import HISTOGRAM_SAMPLE_LIMIT


@contextmanager
def capture_json_logs(name: str = "react-ai-test"):
    """把指定结构化 logger 的输出重定向到内存缓冲，返回可解析的 JSON 行列表。"""
    logger = StructuredLogger(name)
    buf = io.StringIO()
    original = []
    for handler in logger._logger.handlers:
        original.append(handler.stream)
        handler.setStream(buf)
    try:
        yield logger, buf
    finally:
        for handler, stream in zip(logger._logger.handlers, original):
            handler.setStream(stream)


def parse_lines(buf: io.StringIO):
    return [json.loads(line) for line in buf.getvalue().splitlines() if line.strip()]


class TestMetricsCollector(unittest.TestCase):
    def setUp(self):
        self.metrics = MetricsCollector()

    def test_legacy_fields_still_reported(self):
        self.metrics.record_request(tokens=120)
        self.metrics.record_tool_call("query_order", success=False)
        snap = self.metrics.snapshot()
        self.assertEqual(snap["total_requests"], 1)
        self.assertEqual(snap["total_tokens"], 120)
        self.assertEqual(snap["tool_error_counts"]["query_order"], 1)

    def test_labeled_counters_are_independent_series(self):
        self.metrics.inc("intent_extraction_total", {"source": "llm"})
        self.metrics.inc("intent_extraction_total", {"source": "llm"})
        self.metrics.inc("intent_extraction_total", {"source": "fallback"})
        self.assertEqual(self.metrics.counter("intent_extraction_total", {"source": "llm"}), 2.0)
        self.assertEqual(self.metrics.counter("intent_extraction_total", {"source": "fallback"}), 1.0)
        self.assertEqual(len(self.metrics.snapshot()["counters"]["intent_extraction_total"]), 2)

    def test_unlabeled_counter(self):
        self.metrics.inc("checkpoint_fork_total")
        self.assertEqual(self.metrics.counter("checkpoint_fork_total"), 1.0)

    def test_histogram_quantiles_and_totals(self):
        for value in [0.1, 0.2, 0.3, 0.4, 1.0]:
            self.metrics.observe("node_latency_seconds", value, {"node": "intent_recognition"})
        summary = self.metrics.histogram("node_latency_seconds", {"node": "intent_recognition"})
        self.assertEqual(summary["count"], 5)
        self.assertAlmostEqual(summary["sum"], 2.0, places=6)
        self.assertAlmostEqual(summary["quantiles"][0.5], 0.3, places=6)
        self.assertAlmostEqual(summary["quantiles"][0.95], 1.0, places=6)

    def test_histogram_sample_window_is_bounded(self):
        for i in range(HISTOGRAM_SAMPLE_LIMIT + 500):
            self.metrics.observe("ask_user_rounds", i)
        summary = self.metrics.histogram("ask_user_rounds")
        self.assertEqual(summary["count"], HISTOGRAM_SAMPLE_LIMIT + 500)  # 总量全量累计
        self.assertAlmostEqual(summary["sum"], sum(range(HISTOGRAM_SAMPLE_LIMIT + 500)), places=2)
        self.assertLessEqual(summary["quantiles"][0.5], HISTOGRAM_SAMPLE_LIMIT + 500)  # 分位数取自保留样本

    def test_snapshot_and_prometheus_export(self):
        self.metrics.inc("guardrail_action_total", {"action": "block"})
        self.metrics.observe("node_latency_seconds", 0.5, {"node": "ask_user"})
        snap = self.metrics.snapshot()
        self.assertEqual(snap["counters"]["guardrail_action_total"][0]["labels"], {"action": "block"})
        self.assertEqual(snap["histograms"]["node_latency_seconds"][0]["labels"], {"node": "ask_user"})

        text = self.metrics.to_prometheus_text()
        self.assertIn('guardrail_action_total{action="block"} 1.0', text)
        self.assertIn("# TYPE node_latency_seconds summary", text)
        self.assertIn('node_latency_seconds_count{node="ask_user"} 1', text)

    def test_label_values_are_escaped(self):
        self.metrics.inc("decision_total", {"decision": 'WE"IRD'})
        self.assertIn('decision_total{decision="WE\\"IRD"}', self.metrics.to_prometheus_text())

    def test_reset_clears_everything(self):
        self.metrics.inc("decision_total", {"decision": "AUTO_REFUND"})
        self.metrics.observe("ask_user_rounds", 1)
        self.metrics.reset()
        self.assertEqual(self.metrics.snapshot()["counters"], {})
        self.assertEqual(self.metrics.snapshot()["histograms"], {})


class TestStructuredLogger(unittest.TestCase):
    def test_payload_is_single_line_json_with_level(self):
        with capture_json_logs() as (logger, buf):
            logger.info("node_completed", thread_id="default:USR_001:S1", node="intent_recognition")
            logger.warning("session_lock_degraded", thread_id="default:USR_001:S1")
        records = parse_lines(buf)
        self.assertEqual(len(records), 2)
        self.assertEqual(records[0]["level"], "INFO")
        self.assertEqual(records[0]["extra"]["thread_id"], "default:USR_001:S1")
        self.assertEqual(records[1]["level"], "WARNING")  # 级别不再被压平为 INFO，便于日志系统按级别检索
        self.assertTrue(records[0]["time"].endswith("+00:00"))

    def test_bind_carries_correlation_fields(self):
        with capture_json_logs() as (logger, buf):
            bound = logger.bind(thread_id="default:USR_002:S2", session_id="S2")
            bound.info("slots_extracted", missing_slots=["order_id"])
            bound.info("node_completed", node="ask_user")
        records = parse_lines(buf)
        self.assertEqual(records[0]["extra"]["thread_id"], "default:USR_002:S2")
        self.assertEqual(records[1]["extra"]["thread_id"], "default:USR_002:S2")
        self.assertEqual(records[1]["extra"]["node"], "ask_user")

    def test_non_serializable_fields_do_not_raise(self):
        with capture_json_logs() as (logger, buf):
            logger.info("risk_assessment", eligible={"a"})  # 集合不可 JSON 序列化
        self.assertIn("INFO", buf.getvalue())


if __name__ == "__main__":
    unittest.main()
