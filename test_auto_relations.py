import os
import tempfile
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

import server
from relation_store import RelationStore


def bucket(bucket_id, name, *, content=None, **metadata):
    return {
        "id": bucket_id,
        "content": content or f"content-{name}",
        "metadata": {
            "id": bucket_id,
            "name": name,
            "type": "dynamic",
            "tags": [],
            "domain": ["test"],
            **metadata,
        },
    }


class FakeDecay:
    async def ensure_started(self):
        return None


class FakeDehydrator:
    async def dehydrate(self, _content, metadata):
        return f"摘要: {metadata['name']}"


class AutoRelationTests(unittest.IsolatedAsyncioTestCase):
    async def test_sidecar_store_is_symmetric_and_has_no_backfill_api(self):
        with tempfile.TemporaryDirectory() as root:
            store = RelationStore(
                {
                    "buckets_dir": root,
                    "relations": {
                        "db_path": os.path.join(root, "relations.sqlite3"),
                        "similarity_threshold": 0.86,
                        "max_links": 3,
                    },
                }
            )
            inserted = await store.upsert_new_bucket_links(
                "new-bucket",
                [("old-bucket", 0.91), ("another-old", 0.88)],
                "test-model",
            )
            from_new = await store.related("new-bucket")
            from_old = await store.related("old-bucket")
            details = await store.related_details("new-bucket")

        self.assertEqual(inserted, 2)
        self.assertEqual(from_new, [("old-bucket", 0.91), ("another-old", 0.88)])
        self.assertEqual(from_old, [("new-bucket", 0.91)])
        self.assertEqual(details[0]["relation_type"], "semantic")
        self.assertFalse(hasattr(RelationStore, "backfill"))

    async def test_new_bucket_links_only_active_unsealed_candidates(self):
        new = bucket("new", "new")
        visible = bucket("visible", "visible")
        sealed = bucket("sealed", "SEALED-SECRET", sealed=True)
        archived = bucket("archived", "ARCHIVED", type="archived")
        embedding_index = MagicMock(model_name="test-model")
        embedding_index.neighbors_for_bucket = AsyncMock(
            return_value=[
                ("sealed", 0.99),
                ("visible", 0.93),
                ("archived", 0.92),
            ]
        )
        manager = MagicMock(embedding_index=embedding_index)
        manager.get = AsyncMock(return_value=new)
        manager.list_all = AsyncMock(return_value=[new, visible, sealed, archived])
        store = MagicMock(enabled=True, similarity_threshold=0.86, max_links=3)
        store.upsert_new_bucket_links = AsyncMock(return_value=1)

        with (
            patch.object(server, "bucket_mgr", manager),
            patch.object(server, "relation_store", store),
        ):
            selected = await server._auto_link_new_bucket("new")

        self.assertEqual(selected, [("visible", 0.93)])
        self.assertEqual(
            embedding_index.neighbors_for_bucket.await_args.args[1],
            ["visible"],
        )
        store.upsert_new_bucket_links.assert_awaited_once_with(
            "new", [("visible", 0.93)], "test-model"
        )

    async def test_new_creation_hook_runs_without_changing_tool_parameters(self):
        manager = MagicMock()
        manager.create = AsyncMock(return_value="new-id")
        linker = AsyncMock(return_value=[])
        with (
            patch.object(server, "bucket_mgr", manager),
            patch.object(server, "_auto_link_new_bucket", linker),
        ):
            result = await server._append_or_create(
                content="new body",
                tags=[],
                importance=5,
                domain=["test"],
                valence=0.5,
                arousal=0.3,
                allow_append=False,
            )

        self.assertEqual(result, ("new-id", False))
        linker.assert_awaited_once_with("new-id")

    async def test_append_keeps_existing_bucket_category_and_reports_destination(self):
        existing = bucket(
            "existing-id",
            "技术旧桶",
            content="旧正文",
            tags=["技术与创作"],
            domain=["技术与创作"],
            importance=6,
            ai_feeling=False,
        )
        existing["semantic_score"] = 0.93
        manager = MagicMock()
        manager.search = AsyncMock(return_value=[existing])
        manager.update = AsyncMock(return_value=True)
        placement = {}

        with patch.object(server, "bucket_mgr", manager):
            result = await server._append_or_create(
                content="今天吃了一顿饭",
                tags=["日常生活"],
                importance=5,
                domain=["日常生活"],
                valence=0.6,
                arousal=0.4,
                write_result=placement,
            )

        self.assertEqual(result, ("技术旧桶", True))
        self.assertEqual(placement["bucket_id"], "existing-id")
        self.assertEqual(placement["domain"], ["技术与创作"])
        update = manager.update.await_args.kwargs
        self.assertEqual(update["domain"], ["技术与创作"])
        self.assertEqual(update["tags"], ["技术与创作"])
        self.assertTrue(update["content"].startswith("旧正文"))

    async def test_breath_lists_only_visible_active_relations_without_leaking_counts(self):
        source = bucket("source", "source")
        visible = bucket("visible", "visible relation")
        sealed = bucket("sealed", "SEALED-SECRET-NAME", sealed=True)
        archived = bucket("archived", "ARCHIVED-SECRET-NAME", type="archived")
        manager = MagicMock()
        manager.search = AsyncMock(return_value=[source])
        manager.touch = AsyncMock()
        manager.get = AsyncMock(
            side_effect=lambda bucket_id: {
                "visible": visible,
                "sealed": sealed,
                "archived": archived,
            }.get(bucket_id)
        )
        store = MagicMock(enabled=True, max_links=3)
        store.related_details = AsyncMock(
            return_value=[
                {"bucket_id": "sealed", "similarity": 0.99},
                {"bucket_id": "archived", "similarity": 0.98},
                {"bucket_id": "visible", "similarity": 0.92},
            ]
        )
        fact_store = MagicMock(enabled=True)
        fact_store.related_buckets = AsyncMock(return_value=[])
        with (
            patch.object(server, "bucket_mgr", manager),
            patch.object(server, "relation_store", store),
            patch.object(server, "fact_timeline_store", fact_store),
            patch.object(server, "decay_engine", FakeDecay()),
            patch.object(server, "dehydrator", FakeDehydrator()),
            patch.dict(os.environ, {server.RESPONSE_SEAL_ENV: "test-seal"}),
        ):
            result = await server.breath(query="source")

        self.assertIn("【关联记忆】", result)
        self.assertIn("bucket_id: visible", result)
        self.assertIn("visible relation", result)
        self.assertNotIn("sealed", result.lower())
        self.assertNotIn("SEALED-SECRET-NAME", result)
        self.assertNotIn("archived", result.lower())
        self.assertNotIn("ARCHIVED-SECRET-NAME", result)
        self.assertNotIn("还有", result)
        self.assertTrue(result.endswith("\nseal: test-seal"))

    async def test_sealed_source_never_reads_relation_sidecar(self):
        source = bucket("sealed-source", "hidden", sealed=True)
        store = MagicMock(enabled=True, max_links=3)
        store.related_details = AsyncMock(
            return_value=[{"bucket_id": "visible", "similarity": 0.99}]
        )
        with patch.object(server, "relation_store", store):
            result = await server._visible_related_buckets(source)
        self.assertEqual(result, [])
        store.related_details.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
