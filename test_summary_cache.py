import asyncio
import inspect
import os
import sqlite3
import tempfile
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

import server
from dehydrator import Dehydrator
from summary_cache import SummaryCache


def config_for(root: str) -> dict:
    return {
        "buckets_dir": root,
        "dehydration": {
            "api_key": "test-key",
            "base_url": "https://example.invalid",
            "model": "test-model",
            "max_tokens": 100,
            "temperature": 0.0,
        },
        "summary_cache": {
            "enabled": True,
            "db_path": os.path.join(root, "summaries.sqlite3"),
        },
    }


def metadata(bucket_id: str, name: str = "测试桶", **extra) -> dict:
    return {
        "id": bucket_id,
        "name": name,
        "type": "dynamic",
        "domain": ["测试"],
        "tags": [],
        "valence": 0.5,
        "arousal": 0.3,
        **extra,
    }


class SummaryCacheTests(unittest.IsolatedAsyncioTestCase):
    async def test_first_long_read_calls_api_second_read_uses_cache(self):
        with tempfile.TemporaryDirectory() as root:
            dehydrator = Dehydrator(config_for(root))
            api = AsyncMock(return_value="保存好的摘要")
            dehydrator._api_dehydrate = api
            content = "这是一段需要压缩的完整测试记忆。" * 30

            first = await dehydrator.dehydrate(content, metadata("bucket-1"))
            second = await dehydrator.dehydrate(content, metadata("bucket-1"))

            self.assertEqual(dehydrator.summary_cache.count(), 1)

        self.assertEqual(api.await_count, 1)
        self.assertIn("保存好的摘要", first)
        self.assertEqual(first, second)

    async def test_content_change_rebuilds_but_metadata_change_reuses_summary(self):
        with tempfile.TemporaryDirectory() as root:
            dehydrator = Dehydrator(config_for(root))
            api = AsyncMock(side_effect=["第一版摘要", "第二版摘要"])
            dehydrator._api_dehydrate = api
            original = "原始长记忆。" * 40
            changed = original + "内容已经改变。"

            await dehydrator.dehydrate(original, metadata("bucket-2", "旧名字"))
            renamed = await dehydrator.dehydrate(
                original, metadata("bucket-2", "新名字")
            )
            rebuilt = await dehydrator.dehydrate(
                changed, metadata("bucket-2", "新名字")
            )

        self.assertEqual(api.await_count, 2)
        self.assertIn("新名字", renamed)
        self.assertIn("第一版摘要", renamed)
        self.assertIn("第二版摘要", rebuilt)

    async def test_concurrent_first_reads_share_one_api_request(self):
        with tempfile.TemporaryDirectory() as root:
            dehydrator = Dehydrator(config_for(root))

            async def summarize(_content):
                await asyncio.sleep(0.03)
                return "并发摘要"

            api = AsyncMock(side_effect=summarize)
            dehydrator._api_dehydrate = api
            content = "两个窗口同时读取同一个长桶。" * 35
            results = await asyncio.gather(
                dehydrator.dehydrate(content, metadata("bucket-3")),
                dehydrator.dehydrate(content, metadata("bucket-3")),
            )

        self.assertEqual(api.await_count, 1)
        self.assertEqual(results[0], results[1])

    async def test_failed_api_fallback_is_not_cached(self):
        with tempfile.TemporaryDirectory() as root:
            dehydrator = Dehydrator(config_for(root))
            api = AsyncMock(side_effect=RuntimeError("offline"))
            dehydrator._api_dehydrate = api
            content = "API失败时使用本地摘要，但下次仍应重试。" * 30

            await dehydrator.dehydrate(content, metadata("bucket-4"))
            await dehydrator.dehydrate(content, metadata("bucket-4"))

            self.assertEqual(dehydrator.summary_cache.count(), 0)

        self.assertEqual(api.await_count, 2)

    async def test_cache_schema_has_hash_and_summary_but_no_full_source_column(self):
        with tempfile.TemporaryDirectory() as root:
            store = SummaryCache(config_for(root))
            with sqlite3.connect(store.db_path) as connection:
                columns = {
                    row[1]
                    for row in connection.execute("PRAGMA table_info(summary_cache)")
                }

        self.assertIn("content_hash", columns)
        self.assertIn("summary_text", columns)
        self.assertNotIn("content", columns)
        self.assertNotIn("source_text", columns)

    async def test_recall_returns_exact_original_and_sealed_requires_opt_in(self):
        original = "第一行原文。\n第二行的日期是 2026-07-16，标点也要保留！"
        visible = {
            "id": "visible",
            "content": original,
            "metadata": metadata("visible"),
        }
        sealed = {
            "id": "sealed",
            "content": "封存原文",
            "metadata": metadata("sealed", sealed=True),
        }
        manager = MagicMock()
        manager.get = AsyncMock(
            side_effect=lambda bucket_id: {"visible": visible, "sealed": sealed}.get(
                bucket_id
            )
        )
        with (
            patch.object(server, "bucket_mgr", manager),
            patch.dict(os.environ, {server.RESPONSE_SEAL_ENV: "test-seal"}),
        ):
            result = await server.recall("visible")
            hidden = await server.recall("sealed")
            revealed = await server.recall("sealed", include_sealed=True)

        self.assertIn(f"[记忆包 p0001 | 1/1 | 初始正文]\n{original}\nseal: test-seal", result)
        self.assertNotIn("封存原文", hidden)
        self.assertIn("封存原文", revealed)
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

    async def test_trace_delete_removes_cache_without_reading_bucket(self):
        manager = MagicMock()
        manager.delete = AsyncMock(return_value=True)
        cache = MagicMock()
        cache.delete = AsyncMock()
        dehydrator = MagicMock(summary_cache=cache)
        with (
            patch.object(server, "bucket_mgr", manager),
            patch.object(server, "dehydrator", dehydrator),
        ):
            result = await server.trace("gone", delete=True)

        self.assertIn("已遗忘", result)
        manager.delete.assert_awaited_once_with("gone")
        cache.delete.assert_awaited_once_with("gone")


if __name__ == "__main__":
    unittest.main()
