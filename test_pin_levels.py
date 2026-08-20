import inspect
import os
import tempfile
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

import server
from bucket_manager import BucketManager


def make_config(root: str) -> dict:
    return {
        "buckets_dir": root,
        "embeddings": {"enabled": False},
        "history": {"db_path": os.path.join(root, "history.sqlite3")},
        "summary_cache": {
            "enabled": True,
            "db_path": os.path.join(root, "summaries.sqlite3"),
        },
        "wikilink": {"enabled": False},
        "matching": {"fuzzy_threshold": 0, "max_results": 10},
    }


def bucket(bucket_id: str, name: str, *, pin_level=None, **metadata) -> dict:
    meta = {
        "id": bucket_id,
        "name": name,
        "type": "dynamic",
        "pinned": True,
        "domain": ["test"],
        "tags": [],
        "importance": 10,
        "valence": 0.5,
        "arousal": 0.3,
        "created": "2026-01-01T00:00:00",
        "last_active": "2026-01-01T00:00:00",
    }
    if pin_level is not None:
        meta["pin_level"] = pin_level
    meta.update(metadata)
    return {"id": bucket_id, "content": f"content-{name}", "metadata": meta}


class FakeDehydrator:
    async def dehydrate(self, content, metadata):
        return f"summary-{metadata['name']}"


class FakeDecay:
    async def ensure_started(self):
        return None

    def calculate_score(self, _metadata):
        return 1.0


class PinLevelTests(unittest.IsolatedAsyncioTestCase):
    def test_legacy_and_protected_pins_default_to_core(self):
        self.assertEqual(server._pin_level({"pinned": True}), "core")
        self.assertEqual(
            server._pin_level({"pinned": True, "pin_level": "important"}),
            "important",
        )
        self.assertEqual(
            server._pin_level({"protected": True, "pin_level": "important"}),
            "core",
        )
        self.assertEqual(server._pin_level({"pinned": False}), "")

    async def test_create_defaults_to_core_and_unpin_clears_level(self):
        with tempfile.TemporaryDirectory() as root:
            manager = BucketManager(make_config(root))
            bucket_id = await manager.create("原文不变", pinned=True)
            created = await manager.get(bucket_id)
            await manager.update(bucket_id, pinned=False)
            unpinned = await manager.get(bucket_id)

        self.assertEqual(created["metadata"]["pin_level"], "core")
        self.assertNotIn("pin_level", unpinned["metadata"])
        self.assertEqual(unpinned["content"], "原文不变")

    async def test_trace_previews_then_confirms_without_changing_content(self):
        with tempfile.TemporaryDirectory() as root:
            manager = BucketManager(make_config(root))
            bucket_id = await manager.create("完整原文禁止修改", pinned=True)
            with patch.object(server, "bucket_mgr", manager):
                preview = await server.trace(bucket_id, pin_level="important")
                after_preview = await manager.get(bucket_id)
                confirmed = await server.trace(
                    bucket_id,
                    pin_level="important",
                    confirm_pin_level=True,
                )
                after_confirm = await manager.get(bucket_id)

        self.assertIn("尚未修改", preview)
        self.assertEqual(after_preview["metadata"]["pin_level"], "core")
        self.assertIn("重要钉选", confirmed)
        self.assertEqual(after_confirm["metadata"]["pin_level"], "important")
        self.assertEqual(after_confirm["content"], "完整原文禁止修改")

    async def test_protected_and_sealed_buckets_cannot_be_downgraded(self):
        protected = bucket("protected", "protected", protected=True)
        sealed = bucket("sealed", "sealed", sealed=True)
        manager = MagicMock()
        manager.get = AsyncMock(
            side_effect=lambda bucket_id: {
                "protected": protected,
                "sealed": sealed,
            }[bucket_id]
        )
        with patch.object(server, "bucket_mgr", manager):
            protected_result = await server.trace(
                "protected", pin_level="important", confirm_pin_level=True
            )
            sealed_result = await server.trace(
                "sealed", pin_level="core", confirm_pin_level=True
            )

        self.assertIn("保护桶", protected_result)
        self.assertIn("封存桶", sealed_result)
        manager.update.assert_not_called()

    async def test_queryless_breath_surfaces_core_but_not_important(self):
        core = bucket("core", "CORE-VISIBLE")
        important = bucket("important", "IMPORTANT-HIDDEN", pin_level="important")
        manager = MagicMock()
        manager.list_all = AsyncMock(return_value=[core, important])
        manager.touch = AsyncMock()
        with (
            patch.object(server, "bucket_mgr", manager),
            patch.object(server, "dehydrator", FakeDehydrator()),
            patch.object(server, "decay_engine", FakeDecay()),
            patch.dict(os.environ, {server.RESPONSE_SEAL_ENV: "test-seal"}),
        ):
            result = await server.breath()

        self.assertIn("CORE-VISIBLE", result)
        self.assertNotIn("IMPORTANT-HIDDEN", result)

    async def test_important_pin_remains_available_to_explicit_search(self):
        with tempfile.TemporaryDirectory() as root:
            manager = BucketManager(make_config(root))
            bucket_id = await manager.create(
                "只在明确搜索时出现的独特关键词星河灯塔",
                name="按需重要记忆",
                pinned=True,
            )
            await manager.update(bucket_id, pin_level="important")
            relation = MagicMock(enabled=False)
            with (
                patch.object(server, "bucket_mgr", manager),
                patch.object(server, "dehydrator", FakeDehydrator()),
                patch.object(server, "decay_engine", FakeDecay()),
                patch.object(server, "relation_store", relation),
                patch.dict(os.environ, {server.RESPONSE_SEAL_ENV: "test-seal"}),
            ):
                result = await server.breath(query="星河灯塔")

        self.assertIn(f"bucket_id: {bucket_id}", result)
        self.assertIn("按需重要记忆", result)

    async def test_pulse_boot_counts_important_without_expanding_it(self):
        core = bucket("core", "CORE-VISIBLE")
        important = bucket("important", "IMPORTANT-HIDDEN", pin_level="important")
        manager = MagicMock()
        manager.list_all = AsyncMock(return_value=[core, important])
        settings = {
            "fixed_bucket_ids": ["core"],
            "flow_keywords": [],
            "feeling_write_reminder": False,
        }
        with (
            patch.object(server, "bucket_mgr", manager),
            patch.object(server, "dehydrator", FakeDehydrator()),
            patch.object(server.history_retention_engine, "ensure_started", new=AsyncMock()),
            patch.object(
                server,
                "_pulse_boot_mailbox_section",
                new=AsyncMock(return_value="信箱暂无留言。"),
            ),
            patch.dict(server.config, {"pulse_boot": settings}),
            patch.dict(os.environ, {server.RESPONSE_SEAL_ENV: "test-seal"}),
        ):
            result = await server.pulse_boot()

        self.assertIn("【固定层：核心记忆目录】", result)
        self.assertNotIn("自主选择最相关的 1–2 条", result)
        self.assertIn("CORE-VISIBLE", result)
        self.assertNotIn("IMPORTANT-HIDDEN", result)
        self.assertNotIn("bucket_id: important", result)

    def test_trace_only_appends_optional_pin_parameters(self):
        parameters = list(inspect.signature(server.trace).parameters)
        self.assertEqual(
            parameters[-3:], ["pin_level", "confirm_pin_level", "sort_order"]
        )


if __name__ == "__main__":
    unittest.main()
