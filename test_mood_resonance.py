import inspect
import math
import os
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

import server
from bucket_manager import BucketManager


def make_config(root: str) -> dict:
    return {
        "buckets_dir": root,
        "embeddings": {
            "enabled": False,
            "similarity_threshold": 0.45,
        },
        "history": {"db_path": os.path.join(root, "history.sqlite3")},
        "wikilink": {"enabled": False},
        "matching": {"fuzzy_threshold": 0, "max_results": 10},
    }


class FakeDecay:
    async def ensure_started(self):
        return None


class FakeDehydrator:
    async def dehydrate(self, _content, metadata):
        return f"📌 记忆桶: {metadata['name']}"


def memory(bucket_id: str, valence=None, arousal=None):
    metadata = {
        "id": bucket_id,
        "name": bucket_id,
        "type": "dynamic",
        "domain": ["test"],
        "tags": [],
    }
    if valence is not None:
        metadata["valence"] = valence
    if arousal is not None:
        metadata["arousal"] = arousal
    return {"id": bucket_id, "metadata": metadata, "content": bucket_id}


class MoodResonanceTests(unittest.IsolatedAsyncioTestCase):
    def test_distance_ranking_is_normalized_and_skips_invalid_coordinates(self):
        with tempfile.TemporaryDirectory() as root:
            manager = BucketManager(make_config(root))
            ranked = manager.rank_by_mood(
                [
                    memory("far", 1.0, 1.0),
                    memory("near", 0.2, 0.3),
                    memory("middle", 0.5, 0.5),
                    memory("missing", None, 0.3),
                    memory("invalid", 2.0, 0.3),
                ],
                query_valence=0.2,
                query_arousal=0.3,
                limit=10,
            )

        self.assertEqual([item["id"] for item in ranked], ["near", "middle", "far"])
        self.assertEqual(ranked[0]["mood_distance"], 0.0)
        self.assertLessEqual(ranked[-1]["mood_distance"], 1.0)
        self.assertAlmostEqual(
            ranked[1]["mood_distance"],
            math.sqrt((0.2 - 0.5) ** 2 + (0.3 - 0.5) ** 2) / math.sqrt(2),
            places=4,
        )

    async def test_query_keeps_semantic_gate_then_uses_mood_as_primary_sort(self):
        with tempfile.TemporaryDirectory() as root:
            manager = BucketManager(make_config(root))
            far_id = await manager.create(
                content="shared topic far mood",
                name="far",
                valence=0.9,
                arousal=0.9,
                importance=10,
            )
            near_id = await manager.create(
                content="shared topic near mood",
                name="near",
                valence=0.1,
                arousal=0.1,
                importance=1,
            )
            unrelated_id = await manager.create(
                content="shared topic but semantically rejected",
                name="unrelated",
                valence=0.1,
                arousal=0.1,
            )
            scores = {
                far_id: 0.95,
                near_id: 0.60,
                unrelated_id: 0.20,
            }
            with patch.object(
                manager.embedding_index,
                "query_scores",
                new=AsyncMock(return_value=scores),
            ):
                matches = await manager.search(
                    "shared topic",
                    limit=10,
                    query_valence=0.1,
                    query_arousal=0.1,
                    mood_resonance=True,
                )
                strict_domain = await manager.search(
                    "shared topic",
                    limit=10,
                    domain_filter=["missing-domain"],
                    query_valence=0.1,
                    query_arousal=0.1,
                    mood_resonance=True,
                )

        self.assertEqual([item["id"] for item in matches], [near_id, far_id])
        self.assertNotIn(unrelated_id, [item["id"] for item in matches])
        self.assertEqual(strict_domain, [])

    async def test_queryless_mode_combines_domain_feeling_and_sealed_filters(self):
        with tempfile.TemporaryDirectory() as root:
            manager = BucketManager(make_config(root))
            visible_id = await manager.create(
                content="visible feeling",
                name="visible-feeling",
                domain=["work"],
                valence=0.2,
                arousal=0.8,
                ai_feeling=True,
            )
            ordinary_id = await manager.create(
                content="ordinary fact",
                name="ordinary",
                domain=["work"],
                valence=0.2,
                arousal=0.8,
            )
            wrong_domain_id = await manager.create(
                content="wrong domain feeling",
                name="wrong-domain",
                domain=["home"],
                valence=0.2,
                arousal=0.8,
                ai_feeling=True,
            )
            sealed_id = await manager.create(
                content="sealed feeling",
                name="sealed-feeling",
                domain=["work"],
                valence=0.2,
                arousal=0.8,
                ai_feeling=True,
            )
            await manager.update(sealed_id, sealed=True)

            with (
                patch.object(server, "bucket_mgr", manager),
                patch.object(server, "dehydrator", FakeDehydrator()),
                patch.object(server, "decay_engine", FakeDecay()),
                patch.dict(os.environ, {server.RESPONSE_SEAL_ENV: "test-seal"}),
            ):
                hidden = await server.breath(
                    mood_resonance=True,
                    valence=0.2,
                    arousal=0.8,
                    domain="work",
                    feeling_only=True,
                    max_results=10,
                )
                explicit = await server.breath(
                    mood_resonance=True,
                    valence=0.2,
                    arousal=0.8,
                    domain="work",
                    feeling_only=True,
                    include_sealed=True,
                    max_results=10,
                )

        self.assertIn(visible_id, hidden)
        self.assertNotIn(ordinary_id, hidden)
        self.assertNotIn(wrong_domain_id, hidden)
        self.assertNotIn(sealed_id, hidden)
        self.assertIn(visible_id, explicit)
        self.assertIn(sealed_id, explicit)
        self.assertIn("[心境距离: 0.000]", explicit)
        self.assertTrue(explicit.endswith("\nseal: test-seal"))

    async def test_invalid_current_coordinates_fail_before_starting_decay(self):
        decay = FakeDecay()
        decay.ensure_started = AsyncMock()
        with (
            patch.object(server, "decay_engine", decay),
            patch.dict(os.environ, {server.RESPONSE_SEAL_ENV: "test-seal"}),
        ):
            result = await server.breath(
                mood_resonance=True,
                valence=0.2,
            )

        decay.ensure_started.assert_not_awaited()
        self.assertIn("需要同时提供 0~1", result)
        self.assertTrue(result.endswith("\nseal: test-seal"))

    def test_public_signature_appends_one_optional_parameter(self):
        self.assertEqual(
            str(inspect.signature(server.breath)),
            "(query: Optional[str] = None, max_results: int = 3, domain: str = '', valence: float = -1, arousal: float = -1, include_sealed: bool = False, feeling_only: bool = False, mood_resonance: bool = False) -> str",
        )


if __name__ == "__main__":
    unittest.main()
