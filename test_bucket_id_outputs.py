import os
import tempfile
import unittest
from unittest.mock import patch

import server
from bucket_manager import BucketManager


class SummaryOnlyDehydrator:
    async def dehydrate(self, _content, _metadata=None):
        return "summary-without-an-id"


class RunningDecay:
    is_running = True

    async def ensure_started(self):
        return None

    def calculate_score(self, _metadata):
        return 5.0


class BucketIdOutputTests(unittest.IsolatedAsyncioTestCase):
    async def test_breath_and_pulse_expose_bucket_id(self):
        with tempfile.TemporaryDirectory() as root:
            manager = BucketManager({
                "buckets_dir": root,
                "embeddings": {"enabled": False},
                "history": {"db_path": os.path.join(root, "history.sqlite3")},
                "wikilink": {"enabled": False},
                "matching": {"fuzzy_threshold": 0, "max_results": 5},
            })
            bucket_id = await manager.create(
                content="Docker memory service",
                name="diagnostic bucket",
                domain=["test"],
            )

            with (
                patch.object(server, "bucket_mgr", manager),
                patch.object(server, "dehydrator", SummaryOnlyDehydrator()),
                patch.object(server, "decay_engine", RunningDecay()),
                patch.dict(os.environ, {server.RESPONSE_SEAL_ENV: "test-seal"}),
            ):
                search = await server.breath(query="Docker")
                startup = await server.breath()
                listing = await server.pulse()

        self.assertIn(f"bucket_id: {bucket_id}\n", search)
        self.assertIn(f"bucket_id: {bucket_id}\n", startup)
        self.assertIn(f"bucket_id: {bucket_id} |", listing)
        self.assertTrue(search.endswith("\nseal: test-seal"))
        self.assertTrue(startup.endswith("\nseal: test-seal"))
        self.assertTrue(listing.endswith("\nseal: test-seal"))


if __name__ == "__main__":
    unittest.main()
