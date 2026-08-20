import unittest
from unittest.mock import AsyncMock, patch

import server


class FakeBucketManager:
    def __init__(self, semantic_score, ai_feeling=False):
        self.semantic_score = semantic_score
        self.search = AsyncMock(return_value=[{
            "id": "old-bucket",
            "score": 99.0,
            "semantic_score": semantic_score,
            "content": "old content",
            "metadata": {
                "name": "old name",
                "tags": ["7-14"],
                "domain": ["test"],
                "importance": 10,
                "ai_feeling": ai_feeling,
            },
        }])
        self.create = AsyncMock(return_value="new-bucket")
        self.update = AsyncMock(return_value=True)


class AppendThresholdTests(unittest.IsolatedAsyncioTestCase):
    async def _run(self, semantic_score):
        manager = FakeBucketManager(semantic_score)
        with (
            patch.object(server, "bucket_mgr", manager),
            patch.object(server, "_append_timestamp", return_value="2026-07-20T09:20"),
            patch.dict(server.config, {
                "embeddings": {"append_similarity_threshold": 0.62}
            }),
        ):
            result = await server._append_or_create(
                content="new content",
                tags=["7-14"],
                importance=5,
                domain=["test"],
                valence=0.5,
                arousal=0.3,
            )
        return manager, result

    async def test_shared_metadata_cannot_rescue_low_semantic_similarity(self):
        manager, result = await self._run(0.44)
        self.assertEqual(result, ("new-bucket", False))
        manager.create.assert_awaited_once()
        manager.update.assert_not_awaited()

    async def test_high_semantic_similarity_appends_without_rewriting_old_text(self):
        manager, result = await self._run(0.81)
        self.assertEqual(result, ("old name", True))
        manager.update.assert_awaited_once()
        manager.create.assert_not_awaited()
        written = manager.update.await_args.kwargs["content"]
        self.assertEqual(
            written,
            "old content\n\n--- 2026-07-20T09:20 ---\nnew content",
        )
        self.assertTrue(written.startswith("old content"))
        self.assertTrue(manager.update.await_args.kwargs["_require_content_prefix"])

    async def test_feeling_and_fact_buckets_never_cross_append(self):
        manager = FakeBucketManager(0.95, ai_feeling=False)
        with (
            patch.object(server, "bucket_mgr", manager),
            patch.dict(server.config, {
                "embeddings": {"append_similarity_threshold": 0.62}
            }),
        ):
            result = await server._append_or_create(
                content="new feeling",
                tags=[],
                importance=5,
                domain=["test"],
                valence=0.5,
                arousal=0.3,
                ai_feeling=True,
            )

        self.assertEqual(result, ("new-bucket", False))
        manager.update.assert_not_awaited()
        manager.create.assert_awaited_once()
        self.assertTrue(manager.create.await_args.kwargs["ai_feeling"])


if __name__ == "__main__":
    unittest.main()
