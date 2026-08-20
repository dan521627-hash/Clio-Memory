import json
import os
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from fact_timeline_service import FactTimelineService
from fact_timeline_store import FactTimelineStore


class FakeEvaluator:
    def __init__(self, payload=None, error=None):
        self.model = "test-model"
        self.payload = payload or {"candidates": []}
        self.error = error
        self.client = SimpleNamespace(
            chat=SimpleNamespace(
                completions=SimpleNamespace(create=AsyncMock(side_effect=self._reply))
            )
        )

    async def _reply(self, **_kwargs):
        if self.error:
            raise self.error
        message = SimpleNamespace(content=json.dumps(self.payload, ensure_ascii=False))
        return SimpleNamespace(choices=[SimpleNamespace(message=message)])

    @staticmethod
    def _clean_json(raw):
        return json.loads(raw)


class FactTimelineServiceTests(unittest.IsolatedAsyncioTestCase):
    def make_store(self, root):
        return FactTimelineStore(
            {
                "buckets_dir": root,
                "fact_timeline": {
                    "db_path": os.path.join(root, "timeline.sqlite3"),
                    "auto_detect": True,
                },
            }
        )

    async def test_detection_creates_candidate_without_writing_fact(self):
        with tempfile.TemporaryDirectory() as root:
            store = self.make_store(root)
            evaluator = FakeEvaluator(
                {
                    "candidates": [
                        {
                            "fact": "续费日期",
                            "value": "2026-08-20",
                            "previous_value": "2026-08-15",
                            "effective_date": "2026-08-11",
                            "confidence": 0.96,
                            "reason": "续费日改了",
                            "evidence": "续费改到二十号",
                        }
                    ]
                }
            )
            service = FactTimelineService({}, evaluator, store)
            result = await service.process_event(
                "续费改到二十号", "hold", "bucket-one", "event-one"
            )

            self.assertEqual(result["status"], "applied")
            self.assertEqual(store.count(), 0)
            candidates = await store.list_candidates()
            self.assertEqual(len(candidates), 1)
            self.assertEqual(candidates[0]["proposed_value"], "2026-08-20")

    async def test_confirm_mailbox_candidate_writes_version_and_ignore_does_not(self):
        with tempfile.TemporaryDirectory() as root:
            store = self.make_store(root)
            service = FactTimelineService({}, FakeEvaluator(), store)
            first = await store.save_candidate(
                {
                    "fact": "当前城市",
                    "value": "杭州",
                    "effective_date": "2026-08-11",
                    "source_type": "mailbox",
                    "source_ref": "42",
                    "source_excerpt": "我搬到杭州了",
                    "confidence": 0.91,
                    "event_key": "mail-one",
                }
            )
            confirmed = await service.confirm_candidate(first["candidate_id"])
            second = await store.save_candidate(
                {
                    "fact": "常用设备",
                    "value": "新电脑",
                    "effective_date": "2026-08-11",
                    "source_type": "mailbox",
                    "source_ref": "43",
                    "confidence": 0.9,
                    "event_key": "mail-two",
                }
            )
            await service.ignore_candidate(second["candidate_id"])

            self.assertEqual(confirmed["version"]["source_type"], "mailbox")
            self.assertEqual(store.count(), 1)
            self.assertEqual((await store.get_candidate(second["candidate_id"]))["status"], "ignored")

    async def test_duplicate_event_is_not_evaluated_twice(self):
        with tempfile.TemporaryDirectory() as root:
            store = self.make_store(root)
            evaluator = FakeEvaluator()
            service = FactTimelineService({}, evaluator, store)
            await service.process_event("普通记录", "hold", "one", "same-event")
            result = await service.process_event("普通记录", "hold", "one", "same-event")

            self.assertEqual(result["status"], "duplicate")
            self.assertEqual(evaluator.client.chat.completions.create.await_count, 1)

    async def test_evaluator_failure_is_pending_not_raised(self):
        with tempfile.TemporaryDirectory() as root:
            store = self.make_store(root)
            service = FactTimelineService({}, FakeEvaluator(error=RuntimeError("offline")), store)
            result = await service.process_event(
                "续费日期变化", "mailbox", "9", "failed-event"
            )

            self.assertEqual(result["status"], "pending")
            self.assertEqual(store.count(), 0)
            self.assertEqual(await store.list_candidates(), [])


if __name__ == "__main__":
    unittest.main()
