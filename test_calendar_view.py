import unittest

from calendar_view import build_calendar_day, format_calendar_day


class CalendarViewTests(unittest.TestCase):
    def setUp(self):
        self.target = "2026-08-11"

    def test_collects_all_supported_records_for_one_day(self):
        day = build_calendar_day(
            self.target,
            buckets=[
                {
                    "id": "bucket-1",
                    "metadata": {
                        "name": "今天的记忆",
                        "created": "2026-08-10T09:00:00+08:00",
                    },
                    "content": (
                        "--- 2026-08-10T09:00 ---\n昨天\n\n"
                        "--- 2026-08-11T10:30 ---\n今天新增的这一包"
                    ),
                }
            ],
            mailbox=[
                {
                    "message_id": 7,
                    "created_at": "2026-08-11T11:00:00+08:00",
                    "message": "今天的信",
                }
            ],
            behaviors=[
                {
                    "action_id": 8,
                    "action_type": "message",
                    "delivered_at": "2026-08-11T12:00:00+08:00",
                    "content": "静默期发出的话",
                    "context": {"phase": "absence"},
                }
            ],
            tasks=[
                {
                    "task_id": 9,
                    "title": "今天要做的事",
                    "status": "open",
                    "updated_at": "2026-08-11T13:00:00+08:00",
                }
            ],
            treasury=[
                {
                    "entry_id": 10,
                    "entry_type": "income",
                    "amount": "12.00",
                    "reason": "收到奖励",
                    "occurred_at": "2026-08-11T14:00:00+08:00",
                }
            ],
            thoughts=[
                {
                    "canonical_tag": "想靠近",
                    "thought_text": "想靠近一点",
                    "status": "flash",
                    "first_seen": "2026-08-10T15:00:00+08:00",
                    "last_seen": "2026-08-11T15:00:00+08:00",
                }
            ],
            darkflow={
                "cycle_id": 11,
                "created_at": "2026-08-11T16:00:00+08:00",
                "content": "一封暗涌",
            },
            facts=[
                {
                    "fact_label": "出发日期",
                    "versions": [
                        {
                            "version_id": 12,
                            "effective_date": "2026-08-11",
                            "fact_value": "今天出发",
                            "source_bucket_id": "bucket-1",
                        }
                    ],
                }
            ],
        )

        self.assertEqual(day["count"], 8)
        self.assertEqual(
            {item["kind"] for item in day["items"]},
            {
                "memory_segment",
                "mailbox",
                "behavior",
                "task",
                "treasury",
                "thought",
                "darkflow",
                "fact",
            },
        )

    def test_excludes_other_dates_and_silence_nudges(self):
        day = build_calendar_day(
            self.target,
            mailbox=[
                {
                    "message_id": 1,
                    "created_at": "2026-08-10T23:59:00+08:00",
                    "message": "昨天",
                }
            ],
            behaviors=[
                {
                    "action_id": 2,
                    "action_type": "silence_nudge",
                    "delivered_at": "2026-08-11T12:00:00+08:00",
                    "content": "你去干嘛了？",
                    "context": {"phase": "silence"},
                }
            ],
        )
        self.assertEqual(day["items"], [])

    def test_ai_defaults_can_hide_archived_and_sealed_buckets(self):
        buckets = [
            {
                "id": "sealed",
                "metadata": {
                    "name": "封存",
                    "sealed": True,
                    "created": "2026-08-11T08:00:00+08:00",
                },
                "content": "不能默认出现",
            },
            {
                "id": "archived",
                "metadata": {
                    "name": "归档",
                    "type": "archived",
                    "created": "2026-08-11T09:00:00+08:00",
                },
                "content": "不能默认出现",
            },
        ]
        hidden = build_calendar_day(
            self.target,
            buckets=buckets,
            include_archived=False,
            include_sealed=False,
        )
        visible = build_calendar_day(
            self.target,
            buckets=buckets,
            include_archived=True,
            include_sealed=True,
        )
        self.assertEqual(hidden["count"], 0)
        self.assertEqual(visible["count"], 2)

    def test_ai_text_includes_date_and_bucket_id(self):
        day = build_calendar_day(
            self.target,
            buckets=[
                {
                    "id": "bucket-42",
                    "metadata": {
                        "name": "可以定位的记忆",
                        "created": "2026-08-11T08:00:00+08:00",
                    },
                    "content": "完整记录",
                }
            ],
        )
        rendered = format_calendar_day(day)
        self.assertIn("2026-08-11", rendered)
        self.assertIn("bucket_id=bucket-42", rendered)
        self.assertIn("完整记录", rendered)


if __name__ == "__main__":
    unittest.main()
