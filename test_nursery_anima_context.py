from __future__ import annotations

import unittest

from nursery.anima_context import (
    build_child_safe_memory_event,
    build_composite_disposition_context,
)


class CompositeDispositionContextTests(unittest.TestCase):
    def test_keeps_primary_supporting_and_observing_trace_without_raw_evidence(self):
        context = build_composite_disposition_context(
            {
                "days": 30,
                "tendency": {"label": "旧字段不能作为输入"},
                "tendencies": [
                    {"tendency_id": "one", "label": "更愿意靠近", "strength": 0.8, "evidence_count": 4, "last_seen": "2026-09-02"},
                    {"tendency_id": "two", "label": "更习惯先想一想", "strength": 0.6, "evidence_count": 3, "last_seen": "2026-09-01"},
                    {"tendency_id": "three", "label": "更愿意表达", "strength": 0.4, "evidence_count": 2, "last_seen": "2026-08-31"},
                ],
                "recurring_thoughts": [
                    {"event_tag": "想继续陪着", "trace_count": 2, "last_seen": "2026-09-02"}
                ],
                "evidence": [{"summary": "成人原文，绝不能透传"}],
            },
            safe_evidence=[{"category": "关系温度", "summary": "家里刚刚有一次温和的和好", "occurred_at": "2026-09-02"}],
        )
        self.assertEqual(context["primary_tendency"]["id"], "one")
        self.assertEqual([item["id"] for item in context["supporting_tendencies"]], ["two", "three"])
        self.assertEqual(context["emerging_traces"][0]["stage"], "observing")
        self.assertEqual(context["child_safe_evidence"][0]["category"], "关系温度")
        self.assertNotIn("成人原文", str(context))
        self.assertIn("不得写入", "".join(context["rules"]))

    def test_child_safe_memory_event_rejects_raw_anima_fields(self):
        with self.assertRaises(ValueError):
            build_child_safe_memory_event(
                {
                    "source_key": "mailbox:1",
                    "source_version": "v1",
                    "category": "care",
                    "child_safe_summary": "家里刚完成了一次温和的照料。",
                    "occurred_at": "2026-09-02T00:00:00+00:00",
                    "event_summary": "成人原文不可以进来",
                },
                disposition_report={},
            )


if __name__ == "__main__":
    unittest.main()
