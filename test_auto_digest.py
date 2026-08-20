import inspect
import os
import unittest
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import server
from decay_engine import DecayEngine
from digestion_planner import DigestionPlanner


def bucket(
    bucket_id,
    name,
    *,
    last_active="2026-01-01T00:00:00+00:00",
    importance=4,
    activation_count=1,
    **metadata,
):
    return {
        "id": bucket_id,
        "content": f"{name} 的完整正文，日期 2025-01-02，数字 37，不能丢。",
        "metadata": {
            "id": bucket_id,
            "name": name,
            "type": "dynamic",
            "domain": ["测试"],
            "tags": [],
            "importance": importance,
            "activation_count": activation_count,
            "arousal": 0.3,
            "created": last_active,
            "last_active": last_active,
            **metadata,
        },
    }


class FakeEmbeddingIndex:
    def __init__(self, scores=None):
        self.scores = scores or {}
        self.pairwise_scores = AsyncMock(return_value=self.scores)


class GuardedManager:
    def __init__(self, buckets, scores=None):
        self.list_all = AsyncMock(return_value=buckets)
        self.embedding_index = FakeEmbeddingIndex(scores)
        self.create = AsyncMock(side_effect=AssertionError("create is forbidden"))
        self.update = AsyncMock(side_effect=AssertionError("update is forbidden"))
        self.archive = AsyncMock(side_effect=AssertionError("archive is forbidden"))
        self.delete = AsyncMock(side_effect=AssertionError("delete is forbidden"))


class AutoDigestTests(unittest.IsolatedAsyncioTestCase):
    async def test_preview_groups_only_safe_old_low_value_buckets(self):
        old_a = bucket("old-a", "旧学习碎片 A")
        old_b = bucket("old-b", "旧学习碎片 B")
        lone = bucket("lone", "没有同伴的旧碎片")
        manager = GuardedManager(
            [
                old_a,
                old_b,
                lone,
                bucket("recent", "昨天才写", last_active="2026-07-15T00:00:00+00:00"),
                bucket("important", "重要旧事", importance=8),
                bucket("sealed", "封存秘密", sealed=True),
                bucket("pinned", "钉选旧事", pinned=True),
                bucket("feeling", "第一人称感受", ai_feeling=True),
                bucket("todo", "未完待办", tags=["待办"]),
                bucket("trigger", "未来提醒", trigger_date="2026-08-01"),
            ],
            {
                ("old-a", "old-b"): 0.91,
                ("old-a", "lone"): 0.20,
                ("old-b", "lone"): 0.21,
            },
        )
        planner = DigestionPlanner(
            {
                "digestion": {
                    "inactivity_days": 45,
                    "max_importance": 4,
                    "max_activation_count": 2,
                    "group_similarity_threshold": 0.84,
                }
            },
            manager,
        )

        report = await planner.preview(
            datetime(2026, 7, 16, tzinfo=timezone.utc)
        )
        rendered = planner.render(report)

        self.assertEqual(report["mode"], "report_only")
        self.assertEqual(report["eligible_count"], 3)
        self.assertEqual(report["planned_bucket_count"], 2)
        self.assertEqual(report["groups"][0]["bucket_names"], [
            "旧学习碎片 A",
            "旧学习碎片 B",
        ])
        self.assertEqual(report["ungrouped"][0]["name"], "没有同伴的旧碎片")
        self.assertIn("旧学习碎片 A", rendered)
        self.assertIn("日期 2025-01-02", rendered)
        self.assertIn("绝不删除", rendered)
        manager.create.assert_not_awaited()
        manager.update.assert_not_awaited()
        manager.archive.assert_not_awaited()
        manager.delete.assert_not_awaited()

    async def test_decay_is_locked_report_only_even_with_unsafe_config(self):
        candidate = bucket(
            "candidate",
            "低分候选",
            last_active="2025-01-01T00:00:00+00:00",
            importance=1,
            resolved=True,
        )
        manager = GuardedManager([candidate])
        engine = DecayEngine(
            {
                "decay": {
                    "mode": "archive",
                    "threshold": 999,
                    "check_interval_hours": 24,
                }
            },
            manager,
        )

        result = await engine.run_decay_cycle()

        self.assertEqual(result["mode"], "report_only")
        self.assertEqual(result["archived"], 0)
        self.assertEqual(result["would_archive"][0]["name"], "低分候选")
        manager.archive.assert_not_awaited()

    def test_grouping_rejects_transitive_similarity_chain(self):
        groups = DigestionPlanner._components(
            ["a", "b", "c"],
            {
                ("a", "b"): 0.90,
                ("b", "c"): 0.91,
                ("a", "c"): 0.20,
            },
            0.84,
        )
        self.assertEqual(groups, [["a", "b"], ["c"]])

    async def test_digest_preview_tool_is_sealed_and_has_no_parameters(self):
        planner = MagicMock()
        planner.preview = AsyncMock(return_value={"mode": "report_only"})
        planner.render.return_value = "只读演习结果"
        with (
            patch.object(server, "digestion_planner", planner),
            patch.dict(os.environ, {server.RESPONSE_SEAL_ENV: "test-seal"}),
        ):
            result = await server.digest_preview()

        self.assertEqual(str(inspect.signature(server.digest_preview)), "() -> str")
        self.assertIn("只读演习结果", result)
        self.assertTrue(result.endswith("\nseal: test-seal"))


if __name__ == "__main__":
    unittest.main()
