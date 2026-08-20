import inspect
import os
import tempfile
import unittest
from datetime import date, datetime, timezone
from unittest.mock import AsyncMock, patch

import server
from bucket_manager import BucketManager


def make_config(root: str) -> dict:
    return {
        "buckets_dir": root,
        "embeddings": {"enabled": False},
        "history": {"db_path": os.path.join(root, "history.sqlite3")},
        "wikilink": {"enabled": False},
        "matching": {"fuzzy_threshold": 0, "max_results": 10},
    }


def bucket(bucket_id: str, trigger_date: str, **metadata) -> dict:
    return {
        "id": bucket_id,
        "content": f"content-{bucket_id}",
        "metadata": {
            "id": bucket_id,
            "name": bucket_id,
            "type": "dynamic",
            "tags": [],
            "domain": ["test"],
            "created": "2026-01-01T00:00:00",
            "last_active": "2026-01-01T00:00:00",
            "trigger_date": trigger_date,
            **metadata,
        },
    }


class FakeDecay:
    def __init__(self):
        self.ensure_started = AsyncMock()

    def calculate_score(self, _metadata):
        return 1.0


class FakeDehydrator:
    async def analyze(self, _content):
        return {
            "domain": ["测试"],
            "valence": 0.5,
            "arousal": 0.3,
            "tags": [],
            "suggested_name": "前瞻测试",
        }

    async def dehydrate(self, _content, metadata):
        return f"摘要: {metadata['name']}"


class HoldManager:
    def __init__(self):
        self.search = AsyncMock(side_effect=AssertionError("scheduled hold must not merge"))
        self.create = AsyncMock(return_value="scheduled-1")


class PulseManager:
    def __init__(self, buckets):
        self.list_all = AsyncMock(return_value=buckets)


class ProspectiveMemoryTests(unittest.IsolatedAsyncioTestCase):
    def test_date_validation_and_fixed_utc8_boundary(self):
        self.assertEqual(server._normalize_trigger_date("2028-02-29"), "2028-02-29")
        for invalid in ("2026-02-29", "2026-7-01", "16/07/2026", ""):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                server._normalize_trigger_date(invalid)

        near_midnight_utc = datetime(2026, 7, 16, 3, 30, tzinfo=timezone.utc)
        with patch.dict(server.config, {"prospective_memory": {"timezone": "UTC"}}):
            self.assertEqual(
                server._prospective_today(near_midnight_utc),
                date(2026, 7, 16),
            )

    def test_hold_date_prefix_is_server_generated_and_idempotent(self):
        with patch.object(server, "_prospective_today", return_value=date(2026, 7, 21)):
            self.assertEqual(
                server._with_server_write_date("新内容"),
                "【2026-07-21】\n新内容",
            )
            self.assertEqual(
                server._with_server_write_date("【2026-07-21】\n新内容"),
                "【2026-07-21】\n新内容",
            )

    def test_due_filter_orders_today_then_overdue_and_hides_protected_states(self):
        buckets = [
            bucket("overdue-new", "2026-07-15"),
            bucket("today", "2026-07-16"),
            bucket("overdue-old", "2026-07-01"),
            bucket("future", "2026-07-17"),
            bucket("processed", "2026-07-16", trigger_processed=True),
            bucket("sealed", "2026-07-16", sealed=True),
            bucket("invalid", "not-a-date"),
        ]

        due = server._due_prospective_buckets(buckets, date(2026, 7, 16))

        self.assertEqual(
            [item["id"] for item in due],
            ["today", "overdue-old", "overdue-new"],
        )

    async def test_hold_validates_before_work_and_scheduled_write_never_merges(self):
        decay = FakeDecay()
        with patch.object(server, "decay_engine", decay):
            result = await server.hold("unchanged", trigger_date="2026-02-29")
        self.assertIn("YYYY-MM-DD", result)
        decay.ensure_started.assert_not_awaited()

        manager = HoldManager()
        decay = FakeDecay()
        with (
            patch.object(server, "bucket_mgr", manager),
            patch.object(server, "decay_engine", decay),
            patch.object(server, "dehydrator", FakeDehydrator()),
            patch.object(server, "_check_conflicts", new=AsyncMock(return_value=[])),
        ):
            result = await server.hold(
                "正文保持原样",
                trigger_date="2026-07-16",
            )

        manager.search.assert_not_awaited()
        manager.create.assert_awaited_once()
        created = manager.create.await_args.kwargs
        self.assertRegex(created["content"], r"^【\d{4}-\d{2}-\d{2}】\n正文保持原样$")
        self.assertEqual(created["trigger_date"], "2026-07-16")
        self.assertFalse(created["trigger_processed"])
        self.assertIn("[触发:2026-07-16]", result)

    async def test_trace_sets_processes_reenables_and_clears_without_changing_body(self):
        with tempfile.TemporaryDirectory() as root:
            manager = BucketManager(make_config(root))
            bucket_id = await manager.create(content="原始正文，禁止改动", name="future")
            with patch.object(server, "bucket_mgr", manager):
                set_result = await server.trace(
                    bucket_id=bucket_id,
                    trigger_date="2026-07-16",
                )
                processed_result = await server.trace(
                    bucket_id=bucket_id,
                    trigger_processed=1,
                )
                await server.trace(bucket_id=bucket_id, trigger_processed=0)
                await server.trace(bucket_id=bucket_id, trigger_date="2026-07-17")
                reset = await manager.get(bucket_id)
                clear_result = await server.trace(bucket_id=bucket_id, trigger_date="")

            current = await manager.get(bucket_id)

        self.assertIn("trigger_date=2026-07-16", set_result)
        self.assertIn("trigger_processed=True", processed_result)
        self.assertFalse(reset["metadata"]["trigger_processed"])
        self.assertIn("trigger_date=已清除", clear_result)
        self.assertNotIn("trigger_date", current["metadata"])
        self.assertNotIn("trigger_processed", current["metadata"])
        self.assertEqual(current["content"], "原始正文，禁止改动")

    async def test_pulse_boot_does_not_expand_prospective_history(self):
        buckets = [
            bucket("today", "2026-07-16"),
            bucket("overdue", "2026-07-10"),
            bucket("future", "2026-07-17"),
            bucket("processed", "2026-07-16", trigger_processed=True),
            bucket("sealed", "2026-07-16", sealed=True),
        ]
        manager = PulseManager(buckets)
        settings = {"fixed_bucket_ids": [], "flow_keywords": [], "feeling_write_reminder": False}
        with (
            patch.object(server, "bucket_mgr", manager),
            patch.object(server, "dehydrator", FakeDehydrator()),
            patch.object(server, "_prospective_today", return_value=date(2026, 7, 16)),
            patch.object(
                server,
                "_pulse_boot_mailbox_section",
                new=AsyncMock(return_value=""),
            ),
            patch.dict(server.config, {"pulse_boot": settings}),
            patch.dict(os.environ, {server.RESPONSE_SEAL_ENV: "test-seal"}),
        ):
            result = await server.pulse_boot()

        manager.list_all.assert_awaited_once_with(include_archive=True)
        self.assertNotIn("【固定层：核心记忆目录】", result)
        self.assertNotIn("【流动层：信箱最新留言】", result)
        self.assertNotIn("信箱暂无留言。", result)
        self.assertNotIn("bucket_id: today", result)
        self.assertNotIn("bucket_id: overdue", result)
        self.assertNotIn("bucket_id: future", result)
        self.assertNotIn("bucket_id: processed", result)
        self.assertNotIn("bucket_id: sealed", result)
        self.assertTrue(result.endswith("\nseal: test-seal"))

    def test_public_signatures_only_append_optional_parameters(self):
        self.assertEqual(list(inspect.signature(server.hold).parameters)[-1], "trigger_date")
        self.assertEqual(
            list(inspect.signature(server.trace).parameters)[-5:],
            [
                "trigger_date",
                "trigger_processed",
                "pin_level",
                "confirm_pin_level",
                "sort_order",
            ],
        )


if __name__ == "__main__":
    unittest.main()
