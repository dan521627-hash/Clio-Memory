import inspect
import os
import tempfile
import unittest
from datetime import datetime, timezone
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


def bucket(bucket_id: str, *, created: str, ai_feeling=False, **metadata):
    return {
        "id": bucket_id,
        "content": f"content-{bucket_id}",
        "metadata": {
            "id": bucket_id,
            "name": bucket_id,
            "created": created,
            "last_active": created,
            "type": "dynamic",
            "tags": [],
            "domain": ["test"],
            "ai_feeling": ai_feeling,
            **metadata,
        },
    }


class FakeDecay:
    async def ensure_started(self):
        return None

    def calculate_score(self, _metadata):
        return 1.0


class FakeDehydrator:
    async def analyze(self, _content):
        return {
            "domain": ["内心感受"],
            "valence": 0.7,
            "arousal": 0.4,
            "tags": ["第一人称"],
            "suggested_name": "旧日心绪",
        }

    async def dehydrate(self, _content, metadata):
        return f"📌 记忆桶: {metadata['name']}\n核心内容: 一段旧日感受"


class FakePulseManager:
    def __init__(self, buckets):
        self.list_all = AsyncMock(return_value=buckets)


class FeelingEchoTests(unittest.IsolatedAsyncioTestCase):
    async def test_metadata_create_search_and_unmark(self):
        with tempfile.TemporaryDirectory() as root:
            manager = BucketManager(make_config(root))
            ordinary_id = await manager.create(
                content="共同关键词 普通事实",
                name="ordinary",
                ai_feeling=False,
            )
            feeling_id = await manager.create(
                content="共同关键词 我当时感到安心",
                name="feeling",
                ai_feeling=True,
            )

            ordinary = await manager.get(ordinary_id)
            feeling = await manager.get(feeling_id)
            matches = await manager.search(
                "共同关键词",
                limit=10,
                use_semantic=False,
                feeling_only=True,
            )

            self.assertNotIn("ai_feeling", ordinary["metadata"])
            self.assertTrue(feeling["metadata"]["ai_feeling"])
            self.assertEqual([item["id"] for item in matches], [feeling_id])

            self.assertTrue(await manager.update(feeling_id, ai_feeling=False))
            self.assertNotIn("ai_feeling", (await manager.get(feeling_id))["metadata"])

    async def test_hold_marks_feeling_and_trace_can_unmark(self):
        with tempfile.TemporaryDirectory() as root:
            manager = BucketManager(make_config(root))
            with (
                patch.object(server, "bucket_mgr", manager),
                patch.object(server, "decay_engine", FakeDecay()),
                patch.object(server, "dehydrator", FakeDehydrator()),
                patch.object(server, "_check_conflicts", new=AsyncMock(return_value=[])),
            ):
                result = await server.hold("我当时觉得很安心", feeling=True)
                created = (await manager.list_all())[0]
                trace_result = await server.trace(
                    bucket_id=created["id"], feeling=0
                )

            self.assertIn("[感受类]", result)
            self.assertIn("feeling=False", trace_result)
            self.assertNotIn(
                "ai_feeling", (await manager.get(created["id"]))["metadata"]
            )

    async def test_breath_can_surface_only_feeling_memories(self):
        with tempfile.TemporaryDirectory() as root:
            manager = BucketManager(make_config(root))
            ordinary_id = await manager.create(
                content="普通事实不会出现在感受筛选中",
                name="ordinary-fact",
            )
            feeling_id = await manager.create(
                content="我那时有一种安静的欣喜",
                name="quiet-joy",
                ai_feeling=True,
            )
            with (
                patch.object(server, "bucket_mgr", manager),
                patch.object(server, "decay_engine", FakeDecay()),
                patch.object(server, "dehydrator", FakeDehydrator()),
                patch.dict(os.environ, {server.RESPONSE_SEAL_ENV: "test-seal"}),
            ):
                result = await server.breath(feeling_only=True)

        self.assertIn(feeling_id, result)
        self.assertNotIn(ordinary_id, result)
        self.assertTrue(result.endswith("\nseal: test-seal"))

    def test_age_gate_and_protected_states(self):
        now = datetime(2026, 7, 16, tzinfo=timezone.utc)
        buckets = [
            bucket("eligible", created="2026-06-01T00:00:00", ai_feeling=True),
            bucket("recent", created="2026-07-10T00:00:00", ai_feeling=True),
            bucket("ordinary", created="2026-06-01T00:00:00"),
            bucket("sealed", created="2026-06-01T00:00:00", ai_feeling=True, sealed=True),
            bucket("pinned", created="2026-06-01T00:00:00", ai_feeling=True, pinned=True),
            bucket("protected", created="2026-06-01T00:00:00", ai_feeling=True, protected=True),
            bucket("bad-date", created="not-a-date", ai_feeling=True),
        ]

        eligible = server._eligible_pulse_boot_feelings(
            buckets, min_age_days=14, now=now
        )

        self.assertEqual([item["id"] for item in eligible], ["eligible"])

    async def test_pulse_boot_flow_uses_mailbox_not_feeling_buckets(self):
        old_a = bucket("old-a", created="2026-05-01T03:00:00", ai_feeling=True)
        old_b = bucket("old-b", created="2026-05-02T03:00:00", ai_feeling=True)
        manager = FakePulseManager([old_a, old_b])
        settings = {"fixed_bucket_ids": [], "feeling_write_reminder": False}
        with (
            patch.object(server, "bucket_mgr", manager),
            patch.object(server, "dehydrator", FakeDehydrator()),
            patch.object(
                server,
                "_pulse_boot_mailbox_section",
                new=AsyncMock(
                    return_value=(
                        "message_id: 18\n"
                        "时间: 2026-07-26T08:00:00+00:00\n"
                        "最新窗口总和"
                    )
                ),
            ),
            patch.dict(server.config, {"pulse_boot": settings}),
            patch.dict(os.environ, {server.RESPONSE_SEAL_ENV: "test-seal"}),
        ):
            first = await server.pulse_boot()
            second = await server.pulse_boot()

        self.assertNotIn("bucket_id: old-a", first)
        self.assertNotIn("bucket_id: old-b", first)
        self.assertNotIn("bucket_id: old-b", second)
        self.assertIn("【信箱最新留言】", first)
        self.assertIn("message_id: 18", second)
        self.assertIn("最新窗口总和", second)
        self.assertTrue(first.endswith("\nseal: test-seal"))

    def test_tool_signatures_only_append_optional_parameters(self):
        self.assertEqual(
            str(inspect.signature(server.hold)),
            "(content: str, tags: str = '', importance: int = 5, pinned: bool = False, feeling: bool = False, trigger_date: str = '') -> str",
        )
        self.assertEqual(
            str(inspect.signature(server.breath)),
            "(query: Optional[str] = None, max_results: int = 3, domain: str = '', valence: float = -1, arousal: float = -1, include_sealed: bool = False, feeling_only: bool = False, mood_resonance: bool = False) -> str",
        )
        self.assertEqual(list(inspect.signature(server.trace).parameters)[-7:], [
            "sealed",
            "feeling",
            "trigger_date",
            "trigger_processed",
            "pin_level",
            "confirm_pin_level",
            "sort_order",
        ])


if __name__ == "__main__":
    unittest.main()
