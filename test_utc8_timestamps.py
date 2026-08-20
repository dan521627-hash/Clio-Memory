import os
import tempfile
import unittest
from datetime import datetime, timezone

import server
from history_store import HistoryStore
from mailbox_store import MailboxStore
from treasury_store import TreasuryStore
from utils import beijing_now, normalize_beijing_timestamp, now_iso


class FixedUtc8TimestampTests(unittest.IsolatedAsyncioTestCase):
    def test_beijing_clock_ignores_host_and_configured_timezone(self):
        instant = datetime(2026, 8, 1, 0, 30, tzinfo=timezone.utc)
        self.assertEqual(
            beijing_now(instant).isoformat(timespec="minutes"),
            "2026-08-01T08:30+08:00",
        )
        self.assertEqual(server._append_timestamp(instant), "2026-08-01T08:30")
        self.assertEqual(server._prospective_today(instant).isoformat(), "2026-08-01")
        self.assertTrue(now_iso().endswith("+08:00"))

    def test_explicit_timestamps_are_normalized_to_utc8(self):
        self.assertEqual(
            normalize_beijing_timestamp("2026-08-01T00:30:00Z"),
            "2026-08-01T08:30:00+08:00",
        )
        self.assertEqual(
            normalize_beijing_timestamp("2026-08-01T08:30:00"),
            "2026-08-01T08:30:00+08:00",
        )

    async def test_mailbox_history_and_treasury_write_utc8(self):
        with tempfile.TemporaryDirectory() as root:
            mailbox = MailboxStore({
                "buckets_dir": root,
                "mailbox": {"db_path": os.path.join(root, "mailbox.sqlite3")},
            })
            message = await mailbox.add("北京时间测试")
            self.assertTrue(message["created_at"].endswith("+08:00"))

            history = HistoryStore({
                "buckets_dir": root,
                "history": {"db_path": os.path.join(root, "history.sqlite3")},
            })
            await history.snapshot(
                {"id": "utc8-test", "content": "原文", "metadata": {}, "path": ""},
                "test",
            )
            snapshots = await history.list("utc8-test", 1)
            self.assertTrue(snapshots[0]["snapshot_at"].endswith("+08:00"))

            treasury = TreasuryStore({
                "buckets_dir": root,
                "treasury": {"db_path": os.path.join(root, "treasury.sqlite3")},
            })
            entry = await treasury.record(
                "income",
                "1.00",
                "北京时间测试",
                occurred_at="2026-08-01T00:30:00Z",
                source="test",
            )
            self.assertEqual(
                entry["entry"]["occurred_at"],
                "2026-08-01T08:30:00+08:00",
            )
            self.assertTrue(entry["entry"]["created_at"].endswith("+08:00"))


if __name__ == "__main__":
    unittest.main()
