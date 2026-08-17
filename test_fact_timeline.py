import inspect
import os
import sqlite3
import tempfile
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

import server
from fact_timeline_store import FactTimelineStore


def make_bucket(bucket_id: str, *, sealed: bool = False, archived: bool = False) -> dict:
    return {
        "id": bucket_id,
        "content": f"content-{bucket_id}",
        "metadata": {
            "id": bucket_id,
            "name": f"name-{bucket_id}",
            "type": "archived" if archived else "dynamic",
            "sealed": sealed,
            "tags": [],
            "domain": ["test"],
        },
    }


class FactTimelineTests(unittest.IsolatedAsyncioTestCase):
    async def test_existing_database_schema_is_extended_without_changing_rows(self):
        with tempfile.TemporaryDirectory() as root:
            db_path = os.path.join(root, "timeline.sqlite3")
            with sqlite3.connect(db_path) as connection:
                connection.execute(
                    """
                    CREATE TABLE fact_versions (
                        version_id TEXT PRIMARY KEY, fact_key TEXT NOT NULL,
                        fact_label TEXT NOT NULL, fact_value TEXT NOT NULL,
                        effective_date TEXT NOT NULL, valid_to TEXT,
                        is_current INTEGER NOT NULL DEFAULT 0,
                        source_bucket_id TEXT NOT NULL, recorded_at TEXT NOT NULL,
                        UNIQUE (fact_key, effective_date)
                    )
                    """
                )
                connection.execute(
                    "INSERT INTO fact_versions VALUES (?,?,?,?,?,?,?,?,?)",
                    ("v1", "city", "当前城市", "上海", "2026-01-01", None, 1, "b1", "now"),
                )
            store = FactTimelineStore(
                {"buckets_dir": root, "fact_timeline": {"db_path": db_path}}
            )
            rows = await store.versions("city")

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["fact_value"], "上海")
        self.assertEqual(rows[0]["source_type"], "bucket")
        self.assertEqual(rows[0]["source_ref"], "b1")

    async def test_manager_listing_groups_facts_and_supports_search(self):
        with tempfile.TemporaryDirectory() as root:
            store = FactTimelineStore(
                {
                    "buckets_dir": root,
                    "fact_timeline": {"db_path": os.path.join(root, "timeline.sqlite3")},
                }
            )
            await store.record("当前城市", "上海", "2026-01-01", "old")
            await store.record("当前城市", "杭州", "2026-07-01", "new")
            await store.record("常用香味", "白茶", "2026-06-01", "scent")
            all_items = await store.list_facts()
            searched = await store.list_facts("杭州")

        self.assertEqual(len(all_items), 2)
        self.assertEqual(len(searched), 1)
        self.assertEqual(searched[0]["fact_label"], "当前城市")
        self.assertEqual(searched[0]["current"]["fact_value"], "杭州")
        self.assertEqual(len(searched[0]["versions"]), 2)

    async def test_store_orders_versions_and_keeps_one_current(self):
        with tempfile.TemporaryDirectory() as root:
            store = FactTimelineStore(
                {
                    "buckets_dir": root,
                    "fact_timeline": {
                        "db_path": os.path.join(root, "timeline.sqlite3")
                    },
                }
            )
            await store.record("常用香味", "白茶", "2026-07-20", "new")
            await store.record("常用香味", "乌木", "2026-06-01", "old")
            await store.record("常用香味", "雪松", "2026-07-01", "middle")
            rows = await store.versions("常用香味")

        self.assertEqual([row["fact_value"] for row in rows], ["乌木", "雪松", "白茶"])
        self.assertEqual([row["valid_to"] for row in rows], ["2026-07-01", "2026-07-20", None])
        self.assertEqual([row["is_current"] for row in rows], [0, 0, 1])

    async def test_same_fact_links_old_and_new_source_buckets(self):
        with tempfile.TemporaryDirectory() as root:
            store = FactTimelineStore(
                {
                    "buckets_dir": root,
                    "fact_timeline": {
                        "db_path": os.path.join(root, "timeline.sqlite3")
                    },
                }
            )
            await store.record("当前城市", "上海", "2026-01-01", "old-bucket")
            await store.record("当前城市", "杭州", "2026-08-01", "new-bucket")
            from_old = await store.related_buckets("old-bucket")
            from_new = await store.related_buckets("new-bucket")

        self.assertEqual(from_old[0]["bucket_id"], "new-bucket")
        self.assertEqual(from_old[0]["is_current"], 1)
        self.assertEqual(from_new[0]["bucket_id"], "old-bucket")
        self.assertEqual(from_new[0]["is_current"], 0)

    async def test_same_day_conflict_is_rejected_without_overwrite(self):
        with tempfile.TemporaryDirectory() as root:
            store = FactTimelineStore(
                {
                    "buckets_dir": root,
                    "fact_timeline": {
                        "db_path": os.path.join(root, "timeline.sqlite3")
                    },
                }
            )
            await store.record("当前城市", "上海", "2026-07-16", "one")
            with self.assertRaisesRegex(ValueError, "不会自动覆盖"):
                await store.record("当前城市", "杭州", "2026-07-16", "two")
            rows = await store.versions("当前城市")

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["fact_value"], "上海")

    async def test_response_limit_keeps_the_newest_current_version(self):
        with tempfile.TemporaryDirectory() as root:
            store = FactTimelineStore(
                {
                    "buckets_dir": root,
                    "fact_timeline": {
                        "db_path": os.path.join(root, "timeline.sqlite3"),
                        "max_versions_per_response": 2,
                    },
                }
            )
            await store.record("设备", "一代", "2026-01-01", "one")
            await store.record("设备", "二代", "2026-02-01", "two")
            await store.record("设备", "三代", "2026-03-01", "three")
            rows = await store.versions("设备")

        self.assertEqual([row["fact_value"] for row in rows], ["二代", "三代"])
        self.assertEqual(rows[-1]["is_current"], 1)

    async def test_tool_preview_does_not_write_then_confirm_records(self):
        with tempfile.TemporaryDirectory() as root:
            store = FactTimelineStore(
                {
                    "buckets_dir": root,
                    "fact_timeline": {
                        "db_path": os.path.join(root, "timeline.sqlite3")
                    },
                }
            )
            manager = MagicMock()
            manager.get = AsyncMock(return_value=make_bucket("source"))
            with (
                patch.object(server, "fact_timeline_store", store),
                patch.object(server, "bucket_mgr", manager),
                patch.dict(os.environ, {server.RESPONSE_SEAL_ENV: "test-seal"}),
            ):
                preview = await server.timeline(
                    "常用香味", "白茶", "2026-07-16", "source"
                )
                count_after_preview = store.count()
                saved = await server.timeline(
                    "常用香味", "白茶", "2026-07-16", "source", confirm=True
                )

        self.assertIn("尚未写入", preview)
        self.assertEqual(count_after_preview, 0)
        self.assertIn("已记录", saved)
        self.assertIn("白茶（现在）", saved)
        self.assertTrue(saved.endswith("\nseal: test-seal"))

    async def test_sealed_and_archived_sources_leave_no_timeline_trace(self):
        rows = [
            {
                "fact_key": "secret",
                "fact_label": "secret",
                "fact_value": "hidden-value",
                "effective_date": "2026-07-16",
                "is_current": 1,
                "source_bucket_id": "sealed-source",
            },
            {
                "fact_key": "old",
                "fact_label": "old",
                "fact_value": "archived-value",
                "effective_date": "2026-07-15",
                "is_current": 1,
                "source_bucket_id": "archived-source",
            },
        ]
        manager = MagicMock()
        manager.get = AsyncMock(
            side_effect=lambda bucket_id: {
                "sealed-source": make_bucket("sealed-source", sealed=True),
                "archived-source": make_bucket("archived-source", archived=True),
            }[bucket_id]
        )
        with patch.object(server, "bucket_mgr", manager):
            visible = await server._visible_fact_timeline_rows(rows)
        self.assertEqual(visible, [])

    async def test_breath_appends_timeline_without_changing_signature(self):
        source = make_bucket("source")
        manager = MagicMock()
        manager.search = AsyncMock(return_value=[source])
        manager.touch = AsyncMock()
        manager.get = AsyncMock(return_value=source)
        store = MagicMock(enabled=True)
        store.versions_for_bucket = AsyncMock(
            return_value=[
                {
                    "fact_key": "city",
                    "fact_label": "当前城市",
                    "fact_value": "杭州",
                    "effective_date": "2026-07-16",
                    "is_current": 1,
                    "source_bucket_id": "source",
                }
            ]
        )
        dehydrator = MagicMock()
        dehydrator.dehydrate = AsyncMock(return_value="summary")
        decay = MagicMock()
        decay.ensure_started = AsyncMock()
        relation = MagicMock(enabled=False)
        with (
            patch.object(server, "bucket_mgr", manager),
            patch.object(server, "fact_timeline_store", store),
            patch.object(server, "dehydrator", dehydrator),
            patch.object(server, "decay_engine", decay),
            patch.object(server, "relation_store", relation),
            patch.dict(os.environ, {server.RESPONSE_SEAL_ENV: "test-seal"}),
        ):
            result = await server.breath(query="城市")

        self.assertIn("【事实时间线】", result)
        self.assertIn("杭州（现在）", result)
        self.assertEqual(
            list(inspect.signature(server.breath).parameters),
            [
                "query", "max_results", "domain", "valence", "arousal",
                "include_sealed", "feeling_only", "mood_resonance",
            ],
        )
        self.assertEqual(
            list(inspect.signature(server.hold).parameters),
            ["content", "tags", "importance", "pinned", "feeling", "trigger_date"],
        )

    async def test_conflict_warning_suggests_timeline_without_auto_write(self):
        text = server._format_conflict_warning(
            [
                {
                    "old_bucket_id": "old",
                    "old_bucket_name": "旧记录",
                    "old_fact": "住在上海",
                    "new_fact": "搬到杭州",
                    "point": "地点不一致",
                }
            ]
        )
        self.assertIn("可能是事实随时间发生了变化", text)
        self.assertIn("不会自动认定", text)


if __name__ == "__main__":
    unittest.main()
