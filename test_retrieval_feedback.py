import inspect
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import numpy as np

import server
from bucket_manager import BucketManager
from retrieval_feedback import RetrievalFeedbackStore


def feedback_config(root: str) -> dict:
    return {
        "buckets_dir": root,
        "retrieval_feedback": {
            "enabled": True,
            "db_path": str(Path(root) / "retrieval_feedback.sqlite3"),
            "query_similarity_threshold": 0.8,
            "max_adjustment": 5.0,
            "pending_ttl_minutes": 30,
            "max_pending": 32,
        },
    }


def memory(bucket_id: str, content: str = "测试内容") -> dict:
    return {
        "id": bucket_id,
        "content": content,
        "metadata": {
            "name": bucket_id,
            "type": "dynamic",
            "domain": ["test"],
            "tags": [],
            "importance": 5,
            "valence": 0.5,
            "arousal": 0.3,
        },
    }


class RetrievalFeedbackStoreTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.store = RetrievalFeedbackStore(
            feedback_config(self.temp_dir.name), "fake-model", 2
        )

    async def asyncTearDown(self):
        self.temp_dir.cleanup()

    async def test_feedback_is_query_specific_bounded_and_private(self):
        retrieval_id = self.store.begin_search(
            np.array([1.0, 0.0], dtype=np.float32), ["useful", "irrelevant"]
        )
        useful = await self.store.record(retrieval_id, "useful", 1, "recall")
        irrelevant = await self.store.record(
            retrieval_id, "irrelevant", -1, "explicit"
        )

        matching = await self.store.adjustments(
            np.array([1.0, 0.0], dtype=np.float32), ["useful", "irrelevant"]
        )
        unrelated = await self.store.adjustments(
            np.array([0.0, 1.0], dtype=np.float32), ["useful", "irrelevant"]
        )

        self.assertEqual(useful["status"], "recorded")
        self.assertEqual(irrelevant["status"], "recorded")
        self.assertGreater(matching["useful"], 0)
        self.assertLess(matching["irrelevant"], 0)
        self.assertLessEqual(abs(matching["useful"]), 5.0)
        self.assertEqual(unrelated, {})

        with sqlite3.connect(self.store.db_path) as connection:
            columns = {
                row[1]
                for row in connection.execute(
                    "PRAGMA table_info(retrieval_feedback)"
                ).fetchall()
            }
        self.assertNotIn("query", columns)
        self.assertNotIn("query_text", columns)
        self.assertNotIn("content", columns)
        self.assertEqual(self.store.count(), 2)

    async def test_feedback_upserts_instead_of_double_counting(self):
        retrieval_id = self.store.begin_search(
            np.array([1.0, 0.0], dtype=np.float32), ["bucket-a"]
        )
        first = await self.store.record(retrieval_id, "bucket-a", 1, "recall")
        duplicate = await self.store.record(
            retrieval_id, "bucket-a", 1, "explicit"
        )
        changed = await self.store.record(
            retrieval_id, "bucket-a", -1, "explicit"
        )

        self.assertEqual(first["status"], "recorded")
        self.assertEqual(duplicate["status"], "unchanged")
        self.assertEqual(changed["status"], "updated")
        self.assertEqual(self.store.count(), 1)

    async def test_invalid_or_expired_ticket_cannot_rate_another_bucket(self):
        retrieval_id = self.store.begin_search(
            np.array([1.0, 0.0], dtype=np.float32), ["bucket-a"]
        )
        wrong = await self.store.record(
            retrieval_id, "bucket-b", 1, "explicit"
        )
        expired = await self.store.record("missing", "bucket-a", 1, "explicit")

        self.assertEqual(wrong["status"], "not_in_results")
        self.assertEqual(expired["status"], "expired")
        self.assertEqual(self.store.count(), 0)


class RetrievalFeedbackRankingTests(unittest.IsolatedAsyncioTestCase):
    async def test_feedback_reranks_only_after_semantic_gate(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            config = feedback_config(temp_dir)
            config.update(
                {
                    "embeddings": {
                        "enabled": False,
                        "model": "fake-model",
                        "dimensions": 2,
                        "similarity_threshold": 0.45,
                    },
                    "matching": {"fuzzy_threshold": 0, "max_results": 5},
                }
            )
            manager = BucketManager(config)
            first_id = await manager.create("相同主题", name="first")
            second_id = await manager.create("相同主题", name="second")
            blocked_id = await manager.create(
                "完全无关但非常重要", name="blocked", importance=10
            )
            manager.embedding_index.query_scores_with_vector = AsyncMock(
                return_value=(
                    {first_id: 0.9, second_id: 0.9, blocked_id: 0.3},
                    np.array([1.0, 0.0], dtype=np.float32),
                )
            )
            manager.retrieval_feedback.adjustments = AsyncMock(
                return_value={first_id: -4.0, second_id: 4.0, blocked_id: 5.0}
            )

            hits = await manager.search("同义查询", limit=5)

        self.assertEqual([item["id"] for item in hits], [second_id, first_id])
        self.assertNotIn(blocked_id, [item["id"] for item in hits])
        self.assertEqual(hits[0]["feedback_adjustment"], 4.0)
        self.assertTrue(all(item.get("retrieval_id") for item in hits))
        self.assertEqual(hits[0]["retrieval_id"], hits[1]["retrieval_id"])


class RetrievalFeedbackToolTests(unittest.IsolatedAsyncioTestCase):
    async def test_breath_returns_ticket_and_recall_records_positive_feedback(self):
        manager = MagicMock()
        manager.search = AsyncMock(
            return_value=[dict(memory("chosen"), retrieval_id="ticket123")]
        )
        manager.touch = AsyncMock()
        manager.get = AsyncMock(return_value=memory("chosen", "完整原文"))
        feedback_store = MagicMock()
        feedback_store.record = AsyncMock(return_value={"status": "recorded"})
        decay = MagicMock()
        decay.ensure_started = AsyncMock()
        dehydrator = MagicMock()
        dehydrator.dehydrate = AsyncMock(return_value="摘要")

        with (
            patch.object(server, "bucket_mgr", manager),
            patch.object(server, "retrieval_feedback_store", feedback_store),
            patch.object(server, "decay_engine", decay),
            patch.object(server, "dehydrator", dehydrator),
            patch.object(server, "_visible_related_buckets", new=AsyncMock(return_value=[])),
            patch.object(server, "_timeline_for_bucket", new=AsyncMock(return_value="")),
            patch.dict(os.environ, {server.RESPONSE_SEAL_ENV: "test-seal"}),
        ):
            search = await server.breath(query="查询")
            recalled = await server.recall("chosen", retrieval_id="ticket123")

        self.assertTrue(search.startswith("【检索编号提醒】\n"))
        self.assertIn("recall 时请带 retrieval_id=ticket123", search)
        self.assertIn("不要裸调 recall", search)
        self.assertIn("检索反馈: 已记录为本次查询采用", recalled)
        feedback_store.record.assert_awaited_once_with(
            "ticket123", "chosen", rating=1, source="recall"
        )
        self.assertTrue(search.endswith("\nseal: test-seal"))
        self.assertTrue(recalled.endswith("\nseal: test-seal"))

    async def test_negative_feedback_requires_confirmation(self):
        feedback_store = MagicMock()
        feedback_store.record = AsyncMock(return_value={"status": "recorded"})
        with (
            patch.object(server, "retrieval_feedback_store", feedback_store),
            patch.dict(os.environ, {server.RESPONSE_SEAL_ENV: "test-seal"}),
        ):
            preview = await server.feedback(
                "ticket123", "bucket-a", "irrelevant"
            )
            confirmed = await server.feedback(
                "ticket123", "bucket-a", "irrelevant", confirm=True
            )

        self.assertIn("尚未记录负反馈", preview)
        self.assertIn("已记录为不相关", confirmed)
        feedback_store.record.assert_awaited_once_with(
            "ticket123", "bucket-a", rating=-1, source="explicit"
        )
        self.assertTrue(preview.endswith("\nseal: test-seal"))
        self.assertTrue(confirmed.endswith("\nseal: test-seal"))

    def test_public_signatures_are_additive(self):
        self.assertEqual(
            list(inspect.signature(server.recall).parameters),
            [
                "bucket_id",
                "include_sealed",
                "page",
                "page_size",
                "content_id",
                "retrieval_id",
                "segments_per_page",
                "newest_first",
                "limit",
                "before_id",
            ],
        )
        self.assertEqual(
            str(inspect.signature(server.feedback)),
            "(retrieval_id: str, bucket_id: str, rating: str, confirm: bool = False) -> str",
        )


if __name__ == "__main__":
    unittest.main()
