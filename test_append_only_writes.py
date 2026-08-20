import hashlib
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import server
from bucket_manager import BucketManager
from dehydrator import Dehydrator


def make_config(root: str) -> dict:
    return {
        "buckets_dir": root,
        "embeddings": {"enabled": False},
        "history": {"db_path": os.path.join(root, "history.sqlite3")},
        "summary_cache": {
            "enabled": True,
            "db_path": os.path.join(root, "summaries.sqlite3"),
        },
        "wikilink": {
            "enabled": True,
            "use_tags": True,
            "use_domain": True,
            "use_auto_keywords": True,
        },
        "matching": {"fuzzy_threshold": 0, "max_results": 10},
    }


class AppendOnlyWriteTests(unittest.IsolatedAsyncioTestCase):
    async def test_create_and_trace_append_preserve_exact_source_and_snapshot(self):
        with tempfile.TemporaryDirectory() as root:
            manager = BucketManager(make_config(root))
            original = "AI 今天说了原话。\n末尾空格要留住  "
            bucket_id = await manager.create(
                content=original,
                name="exact-source",
                tags=["身份"],
                domain=["AI 自我"],
            )
            self.assertEqual((await manager.get(bucket_id))["content"], original)
            self.assertNotIn("[[", (await manager.get(bucket_id))["content"])

            with (
                patch.object(server, "bucket_mgr", manager),
                patch.object(
                    server,
                    "_append_timestamp",
                    side_effect=["2026-07-20T09:20", "2026-07-20T09:21"],
                ),
            ):
                first = await server.trace(bucket_id, content="第一次追加", append=True)
                second = await server.trace(bucket_id, content="第二次追加", append=True)

            expected_first = (
                original + "\n\n--- 2026-07-20T09:20 ---\n第一次追加"
            )
            expected_second = (
                expected_first + "\n\n--- 2026-07-20T09:21 ---\n第二次追加"
            )
            current = await manager.get(bucket_id)
            history = await manager.get_history(bucket_id, 10)
            self.assertIn("content=已追加", first + second)
            self.assertEqual(current["content"], expected_second)
            self.assertTrue(current["content"].startswith(original))
            self.assertEqual(history[0]["content"], expected_first)
            self.assertEqual(history[1]["content"], original)

    async def test_append_or_create_never_calls_a_model_merge(self):
        manager = MagicMock()
        manager.search = AsyncMock(
            return_value=[
                {
                    "id": "old-id",
                    "content": "旧正文逐字保留",
                    "semantic_score": 0.95,
                    "metadata": {
                        "name": "旧桶",
                        "tags": [],
                        "domain": ["测试"],
                        "importance": 5,
                    },
                }
            ]
        )
        manager.update = AsyncMock(return_value=True)
        manager.create = AsyncMock(return_value="new-id")
        forbidden_dehydrator = MagicMock()
        forbidden_dehydrator.merge.side_effect = AssertionError("model merge was called")
        with (
            patch.object(server, "bucket_mgr", manager),
            patch.object(server, "dehydrator", forbidden_dehydrator),
            patch.object(server, "_append_timestamp", return_value="2026-07-20T09:20"),
            patch.dict(
                server.config,
                {"embeddings": {"append_similarity_threshold": 0.62}},
            ),
        ):
            result = await server._append_or_create(
                content="新内容不压缩",
                tags=[],
                importance=5,
                domain=["测试"],
                valence=0.5,
                arousal=0.3,
            )

        self.assertEqual(result, ("旧桶", True))
        forbidden_dehydrator.merge.assert_not_called()
        written = manager.update.await_args.kwargs["content"]
        self.assertEqual(
            written,
            "旧正文逐字保留\n\n--- 2026-07-20T09:20 ---\n新内容不压缩",
        )

    async def test_digest_api_failure_uses_local_fallback(self):
        with tempfile.TemporaryDirectory() as root:
            config = make_config(root)
            config["dehydration"] = {
                "api_key": "test-key",
                "base_url": "https://example.invalid",
                "model": "test-model",
            }
            dehydrator = Dehydrator(config)
            dehydrator._api_digest = AsyncMock(side_effect=RuntimeError("offline"))
            fallback = [{"name": "本地兜底", "content": "原内容"}]
            dehydrator._local_digest = AsyncMock(return_value=fallback)

            result = await dehydrator.digest("这是一段足够长的测试归档内容，用于确认接口失败后仍走本地兜底。")

        self.assertEqual(result, fallback)
        dehydrator._api_digest.assert_awaited_once()
        dehydrator._local_digest.assert_awaited_once()

    async def test_summary_cache_never_changes_markdown_source(self):
        with tempfile.TemporaryDirectory() as root:
            config = make_config(root)
            config["dehydration"] = {
                "api_key": "test-key",
                "base_url": "https://example.invalid",
                "model": "test-model",
            }
            manager = BucketManager(config)
            bucket_id = await manager.create(
                content="摘要只能放旁边，绝对不能覆盖这段原文。" * 30,
                name="summary-sidecar",
                domain=["测试"],
            )
            bucket = await manager.get(bucket_id)
            path = bucket["path"]
            before = hashlib.sha256(Path(path).read_bytes()).hexdigest()
            dehydrator = Dehydrator(config)
            dehydrator._api_dehydrate = AsyncMock(return_value="旁路摘要")

            summary = await dehydrator.dehydrate(bucket["content"], bucket["metadata"])
            after = hashlib.sha256(Path(path).read_bytes()).hexdigest()

        self.assertIn("旁路摘要", summary)
        self.assertEqual(after, before)
        self.assertFalse(hasattr(Dehydrator, "merge"))

    async def test_touch_and_archive_preserve_exact_body(self):
        with tempfile.TemporaryDirectory() as root:
            manager = BucketManager(make_config(root))
            original = "正文末尾的空格和空行也属于原文。  \n\n"
            bucket_id = await manager.create(
                content=original,
                name="metadata-write-preserves-body",
                domain=["测试"],
            )

            await manager.touch(bucket_id)
            self.assertEqual((await manager.get(bucket_id))["content"], original)
            self.assertTrue(await manager.archive(bucket_id))
            self.assertEqual((await manager.get(bucket_id))["content"], original)


if __name__ == "__main__":
    unittest.main()
