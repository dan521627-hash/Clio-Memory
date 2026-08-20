import json
import os
import tempfile
import unittest
from unittest.mock import patch

import server
from bucket_manager import BucketManager
from conflict_detector import ConflictDetector
from decay_engine import DecayEngine


SECRET_NAME = "SEALED-NAME-OMEGA-7291"
SECRET_CONTENT = "SEALED-CONTENT-OMEGA-7291 每月预算是3000元"
SECRET_QUERY = "SEALED-CONTENT-OMEGA-7291"
SECRET_TAG = "SEALED-TAG-OMEGA-7291"
SECRET_DOMAIN = "SEALED-DOMAIN-OMEGA-7291"


def make_config(root: str) -> dict:
    return {
        "buckets_dir": root,
        "embeddings": {"enabled": False},
        "history": {"db_path": os.path.join(root, "history.sqlite3")},
        "wikilink": {"enabled": False},
        "matching": {"fuzzy_threshold": 30, "max_results": 10},
        "decay": {"threshold": -1, "check_interval_hours": 24},
    }


class FakeDehydrator:
    api_available = False

    async def dehydrate(self, content, metadata=None):
        metadata = metadata or {}
        return f"{metadata.get('name', '')}|{metadata.get('id', '')}|{content}"

    async def merge(self, old_content, new_content):
        return old_content + "\n" + new_content


class FakeDecay:
    is_running = True

    async def ensure_started(self):
        return None

    def calculate_score(self, metadata):
        return float(metadata.get("importance", 5))


def assert_secret_absent(testcase, text: str, bucket_id: str):
    for marker in (
        SECRET_NAME,
        SECRET_CONTENT,
        SECRET_QUERY,
        SECRET_TAG,
        SECRET_DOMAIN,
        bucket_id,
    ):
        testcase.assertNotIn(marker, text)


class SealedBucketTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.config = make_config(self.temp_dir.name)
        self.manager = BucketManager(self.config)
        self.visible_id = await self.manager.create(
            content="普通可见内容",
            name="visible-memory",
            tags=["visible"],
            domain=["visible-domain"],
            importance=5,
        )
        self.sealed_id = await self.manager.create(
            content=SECRET_CONTENT,
            name=SECRET_NAME,
            tags=[SECRET_TAG],
            domain=[SECRET_DOMAIN],
            importance=9,
        )
        self.assertTrue(await self.manager.update(self.sealed_id, sealed=True))

    async def asyncTearDown(self):
        self.temp_dir.cleanup()

    async def test_default_list_search_and_stats_leave_no_trace(self):
        default_list = await self.manager.list_all(include_archive=True)
        explicit_list = await self.manager.list_all(
            include_archive=True, include_sealed=True
        )
        self.assertEqual([bucket["id"] for bucket in default_list], [self.visible_id])
        self.assertIn(self.sealed_id, [bucket["id"] for bucket in explicit_list])

        default_hits = await self.manager.search(
            SECRET_QUERY, limit=10, use_semantic=False
        )
        explicit_hits = await self.manager.search(
            SECRET_QUERY,
            limit=10,
            use_semantic=False,
            include_sealed=True,
        )
        self.assertNotIn(self.sealed_id, [bucket["id"] for bucket in default_hits])
        self.assertIn(self.sealed_id, [bucket["id"] for bucket in explicit_hits])

        default_stats = await self.manager.get_stats()
        explicit_stats = await self.manager.get_stats(include_sealed=True)
        self.assertEqual(default_stats["dynamic_count"], 1)
        self.assertEqual(explicit_stats["dynamic_count"], 2)
        self.assertNotIn(SECRET_DOMAIN, default_stats["domains"])
        self.assertIn(SECRET_DOMAIN, explicit_stats["domains"])
        self.assertLess(default_stats["total_size_kb"], explicit_stats["total_size_kb"])

    async def test_all_retrieval_and_display_tools_hide_sealed_bucket(self):
        with (
            patch.object(server, "bucket_mgr", self.manager),
            patch.object(server, "dehydrator", FakeDehydrator()),
            patch.object(server, "decay_engine", FakeDecay()),
            patch.object(server.random, "random", return_value=1.0),
        ):
            default_search = await server.breath(query=SECRET_QUERY, max_results=10)
            explicit_search = await server.breath(
                query=SECRET_QUERY,
                max_results=10,
                include_sealed=True,
            )
            startup_surface = await server.breath(include_sealed=True)
            listing = await server.pulse(include_archive=True)
            health = await server.health_check(None)

        assert_secret_absent(self, default_search, self.sealed_id)
        assert_secret_absent(self, startup_surface, self.sealed_id)
        assert_secret_absent(self, listing, self.sealed_id)
        self.assertIn(SECRET_CONTENT, explicit_search)
        self.assertIn(SECRET_NAME, explicit_search)
        health_payload = json.loads(health.body)
        self.assertEqual(health_payload["buckets"], 1)

    async def test_background_and_write_helpers_ignore_sealed_bucket(self):
        decay = DecayEngine(self.config, self.manager)
        decay_result = await decay.run_decay_cycle()
        self.assertEqual(decay_result["checked"], 1)

        async def scores(_query):
            return {self.sealed_id: 0.99, self.visible_id: 0.1}

        self.manager.embedding_index.query_scores = scores
        detector = ConflictDetector(
            self.config, self.manager, FakeDehydrator()
        )
        self.assertEqual(
            await detector.detect("SEALED-CONTENT-OMEGA-7291 每月预算是5000元"),
            [],
        )

        old_sealed = await self.manager.get(self.sealed_id)
        with (
            patch.object(server, "bucket_mgr", self.manager),
            patch.object(server, "dehydrator", FakeDehydrator()),
        ):
            result_id, appended = await server._append_or_create(
                content=SECRET_CONTENT,
                tags=[SECRET_TAG],
                importance=5,
                domain=[SECRET_DOMAIN],
                valence=0.5,
                arousal=0.3,
                name="new-visible-copy",
            )
        self.assertFalse(appended)
        self.assertNotEqual(result_id, self.sealed_id)
        self.assertEqual((await self.manager.get(self.sealed_id))["content"], old_sealed["content"])

    async def test_sealed_and_pinned_are_mutually_exclusive(self):
        pinned_id = await self.manager.create(
            content="pinned then sealed",
            name="pinned-sealed-test",
            domain=["test"],
            pinned=True,
        )
        with patch.object(server, "bucket_mgr", self.manager):
            sealed_response = await server.trace(bucket_id=pinned_id, sealed=1)
            pin_rejected = await server.trace(bucket_id=pinned_id, pinned=1)

        sealed_bucket = await self.manager.get(pinned_id)
        self.assertIn("sealed=True", sealed_response)
        self.assertTrue(sealed_bucket["metadata"]["sealed"])
        self.assertFalse(sealed_bucket["metadata"]["pinned"])
        self.assertIn("不能设置为钉选", pin_rejected)
        self.assertFalse(await self.manager.update(pinned_id, pinned=True))

        with patch.object(server, "bucket_mgr", self.manager):
            unsealed_response = await server.trace(
                bucket_id=pinned_id, sealed=0, pinned=1
            )
        unsealed_bucket = await self.manager.get(pinned_id)
        self.assertIn("sealed=False", unsealed_response)
        self.assertFalse(unsealed_bucket["metadata"]["sealed"])
        self.assertTrue(unsealed_bucket["metadata"]["pinned"])


if __name__ == "__main__":
    unittest.main()
