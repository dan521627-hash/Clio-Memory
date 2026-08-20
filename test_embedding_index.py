import asyncio
import tempfile
import unittest
from pathlib import Path

import numpy as np

from bucket_manager import BucketManager
from embedding_index import EmbeddingIndex


class FakeEmbeddingModel:
    @staticmethod
    def _vector(text: str):
        if "cat" in text or "pet" in text:
            return np.array([1.0, 0.0], dtype=np.float32)
        return np.array([0.0, 1.0], dtype=np.float32)

    def passage_embed(self, texts, batch_size=8):
        return (self._vector(text) for text in texts)

    def query_embed(self, texts):
        return (self._vector(text) for text in texts)


class EmbeddingIndexTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        config = {
            "buckets_dir": str(self.root),
            "embeddings": {
                "enabled": True,
                "model": "fake-model",
                "dimensions": 2,
                "batch_size": 2,
                "cache_dir": str(self.root / "models"),
                "db_path": str(self.root / "embeddings.sqlite3"),
            },
        }
        self.index = EmbeddingIndex(config)
        self.index._model = FakeEmbeddingModel()

    async def asyncTearDown(self):
        self.temp_dir.cleanup()

    async def test_backfill_query_update_and_delete(self):
        buckets = [
            {
                "id": "cat-memory",
                "metadata": {"name": "feline", "domain": ["life"], "tags": []},
                "content": "a cat lives here",
            },
            {
                "id": "work-memory",
                "metadata": {"name": "job", "domain": ["work"], "tags": []},
                "content": "a project deadline",
            },
        ]

        result = await self.index.backfill(buckets)
        self.assertEqual(result["updated"], 2)
        self.assertEqual(self.index.count(), 2)

        scores = await self.index.query_scores("my pet")
        self.assertGreater(scores["cat-memory"], scores["work-memory"])

        unchanged = await self.index.upsert_bucket(buckets[0])
        self.assertFalse(unchanged)
        buckets[0]["content"] = "a project deadline"
        updated = await self.index.upsert_bucket(buckets[0])
        self.assertTrue(updated)

        await self.index.delete("cat-memory")
        self.assertEqual(self.index.count(), 1)

    async def test_durable_queue_survives_until_worker_processes_it(self):
        bucket = {
            "id": "queued-memory",
            "metadata": {"name": "queued", "domain": ["life"], "tags": []},
            "content": "a cat waits here",
        }
        self.index.set_bucket_loader(
            lambda bucket_id: bucket if bucket_id == bucket["id"] else None
        )

        queued = await self.index.enqueue_bucket(bucket)

        self.assertTrue(queued)
        self.assertEqual(self.index.pending_count(), 1)
        self.assertEqual(self.index.count(), 0)

        result = await self.index.process_queue()

        self.assertEqual(result["processed"], 1)
        self.assertEqual(result["remaining"], 0)
        self.assertEqual(self.index.count(), 1)

    async def test_newer_queue_write_is_not_removed_by_older_work(self):
        bucket = {
            "id": "changing-memory",
            "metadata": {"name": "changing", "domain": ["life"], "tags": []},
            "content": "a cat waits here",
        }
        self.index.set_bucket_loader(
            lambda bucket_id: bucket if bucket_id == bucket["id"] else None
        )
        await self.index.enqueue_bucket(bucket)
        bucket["content"] = "a project deadline"

        first = await self.index.process_queue()
        second = await self.index.process_queue()

        self.assertEqual(first["processed"], 0)
        self.assertEqual(second["processed"], 1)
        scores = await self.index.query_scores("project")
        self.assertIn(bucket["id"], scores)


class HybridSearchTests(unittest.IsolatedAsyncioTestCase):
    def test_paraphrase_retrieval_intent_is_normalized(self):
        query = "换一种完全不同的说法也能找到以前的内容"
        self.assertEqual(
            BucketManager._normalize_semantic_query(query),
            "同义表达 语义搜索 向量相似度 breath 检索 记忆召回",
        )
        self.assertEqual(
            BucketManager._normalize_semantic_query("为什么每次工具请求都重新握手"),
            "为什么每次工具请求都重新握手",
        )

    async def test_semantic_match_can_recall_without_keyword_overlap(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            config = {
                "buckets_dir": temp_dir,
                "embeddings": {"enabled": False, "similarity_threshold": 0.35},
                "matching": {"fuzzy_threshold": 50, "max_results": 5},
                "scoring_weights": {
                    "semantic_similarity": 10.0,
                    "topic_relevance": 4.0,
                    "emotion_resonance": 2.0,
                    "time_proximity": 1.5,
                    "importance": 1.0,
                },
            }
            manager = BucketManager(config)
            bucket_id = await manager.create(
                content="The laptop must stay awake when its lid is closed.",
                name="power policy",
                tags=["sleep"],
                domain=["system"],
            )

            async def semantic_scores(_query):
                return {bucket_id: 0.91}

            async def no_segment_scores(_query, _buckets):
                return {}, None

            manager.embedding_index.enabled = True
            manager.embedding_index.query_scores = semantic_scores
            manager.embedding_index.query_segment_matches = no_segment_scores
            hits = await manager.search("unrelated wording", limit=3)

            self.assertEqual(hits[0]["id"], bucket_id)
            self.assertEqual(hits[0]["semantic_score"], 0.91)

    async def test_semantic_failure_uses_original_keyword_query(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            config = {
                "buckets_dir": temp_dir,
                "embeddings": {"enabled": False, "similarity_threshold": 0.35},
                "matching": {"fuzzy_threshold": 0, "max_results": 5},
            }
            manager = BucketManager(config)
            bucket_id = await manager.create(
                content="换一种完全不同的说法也能找到以前的内容",
                name="exact keyword fallback",
            )

            async def unavailable(_query):
                raise RuntimeError("model unavailable")

            manager.embedding_index.query_scores = unavailable
            hits = await manager.search(
                "换一种完全不同的说法也能找到以前的内容", limit=3
            )

            self.assertEqual(hits[0]["id"], bucket_id)
            self.assertNotIn("semantic_score", hits[0])

    async def test_semantic_floor_blocks_high_importance_unrelated_bucket(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            config = {
                "buckets_dir": temp_dir,
                "embeddings": {"enabled": False, "similarity_threshold": 0.45},
                "matching": {"fuzzy_threshold": 0, "max_results": 5},
                "scoring_weights": {
                    "semantic_similarity": 6.0,
                    "topic_relevance": 4.0,
                    "emotion_resonance": 2.0,
                    "time_proximity": 1.5,
                    "importance": 10.0,
                },
            }
            manager = BucketManager(config)
            unrelated_id = await manager.create(
                content="high importance but unrelated romance",
                importance=10,
            )

            async def semantic_scores(_query):
                return {unrelated_id: 0.31}

            async def no_segment_scores(_query, _buckets):
                return {}, None

            manager.embedding_index.query_scores = semantic_scores
            manager.embedding_index.query_segment_matches = no_segment_scores
            hits = await manager.search("technical Docker migration", limit=3)

            self.assertEqual(hits, [])


if __name__ == "__main__":
    unittest.main()
