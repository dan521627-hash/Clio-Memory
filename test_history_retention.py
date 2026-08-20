import json
import os
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

from history_retention import HistoryRetentionEngine
from history_store import HistoryStore


class FakeBucketManager:
    def __init__(self, buckets):
        self.list_all = AsyncMock(return_value=buckets)


class HistoryRetentionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.store = HistoryStore(
            {
                "buckets_dir": self.temp_dir.name,
                "history": {
                    "db_path": os.path.join(self.temp_dir.name, "history.sqlite3")
                },
            }
        )

    async def asyncTearDown(self):
        self.temp_dir.cleanup()

    def insert_snapshot(
        self,
        bucket_id: str,
        snapshot_at: str,
        operation_type: str = "content_replace",
        metadata: dict | None = None,
    ) -> int:
        with sqlite3.connect(self.store.db_path) as connection:
            cursor = connection.execute(
                """
                INSERT INTO bucket_history (
                    bucket_id, snapshot_at, operation_type,
                    content, metadata_json, source_path
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    bucket_id,
                    snapshot_at,
                    operation_type,
                    "完整旧版",
                    json.dumps(metadata or {}, ensure_ascii=False),
                    "",
                ),
            )
            return int(cursor.lastrowid)

    async def test_preview_keeps_recent_three_delete_and_important_snapshots(self):
        now = datetime(2026, 7, 16, tzinfo=timezone.utc)
        old = (now - timedelta(days=20)).isoformat()
        recent = (now - timedelta(days=2)).isoformat()

        ordinary_ids = [
            self.insert_snapshot("ordinary", old) for _ in range(5)
        ]
        for _ in range(5):
            self.insert_snapshot("recent", recent)
        self.insert_snapshot("deleted", old, operation_type="delete")
        for _ in range(3):
            self.insert_snapshot("deleted", old)
        self.insert_snapshot("snapshot-important", old, metadata={"pinned": True})
        for _ in range(3):
            self.insert_snapshot("snapshot-important", old)
        for _ in range(4):
            self.insert_snapshot("currently-important", old)
        self.insert_snapshot("bad-time", "not-a-date")

        report = await self.store.preview_retention(
            retention_days=10,
            min_versions_per_bucket=3,
            protected_bucket_ids={"currently-important"},
            now=now,
        )

        self.assertEqual(
            sorted(item["snapshot_id"] for item in report["candidates"]),
            sorted(ordinary_ids[:2]),
        )
        self.assertEqual(report["candidate_count"], 2)
        self.assertEqual(report["protected_counts"]["delete_snapshot"], 1)
        self.assertEqual(report["protected_counts"]["important_memory"], 2)
        self.assertEqual(report["protected_counts"]["invalid_timestamp"], 1)
        self.assertEqual(report["mode"], "report_only")

    async def test_engine_is_locked_report_only_and_has_no_delete_path(self):
        store = AsyncMock()
        store.preview_retention.return_value = {
            "mode": "report_only",
            "checked": 4,
            "candidate_count": 1,
            "candidates": [
                {
                    "snapshot_id": 1,
                    "bucket_id": "ordinary",
                    "snapshot_at": "2026-01-01T00:00:00+00:00",
                    "operation_type": "content_replace",
                }
            ],
        }
        manager = FakeBucketManager(
            [{"id": "protected", "metadata": {"pinned": True}}]
        )
        engine = HistoryRetentionEngine(
            {
                "history": {
                    "retention": {
                        "mode": "delete_expired",
                        "retention_days": 10,
                        "min_versions_per_bucket": 3,
                    }
                }
            },
            manager,
            store,
        )

        report = await engine.run_cycle()

        self.assertEqual(engine.mode, "report_only")
        self.assertEqual(report["mode"], "report_only")
        self.assertEqual(
            store.preview_retention.await_args.kwargs["protected_bucket_ids"],
            {"protected"},
        )
        self.assertFalse(hasattr(HistoryStore, "delete_expired"))

    async def test_disabled_engine_does_not_read_history_or_buckets(self):
        store = AsyncMock()
        manager = FakeBucketManager([])
        engine = HistoryRetentionEngine(
            {"history": {"retention": {"enabled": False}}}, manager, store
        )

        report = await engine.run_cycle()

        self.assertFalse(report["enabled"])
        manager.list_all.assert_not_awaited()
        store.preview_retention.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
