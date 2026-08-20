import hashlib
import os
import re
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

import server
from bucket_manager import BucketManager


def make_config(root: str) -> dict:
    return {
        "buckets_dir": root,
        "embeddings": {"enabled": False},
        "history": {"db_path": os.path.join(root, "history.sqlite3")},
        "wikilink": {"enabled": False},
        "matching": {"fuzzy_threshold": 0, "max_results": 10},
    }


class BucketSplitTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.manager = BucketManager(make_config(self.temp_dir.name))
        self.source = (
            "初始正文\n\n"
            "--- 2026-07-17T09:00 ---\n第一段\n\n"
            "--- 2026-07-18T09:00 ---\n第二段\n\n"
            "--- 2026-07-19T09:00 ---\n第三段"
        )
        self.bucket_id = await self.manager.create(
            content=self.source,
            name="待拆分长桶",
            tags=["测试"],
            domain=["测试"],
        )

    async def asyncTearDown(self):
        self.temp_dir.cleanup()

    async def test_preview_is_read_only_and_confirm_copies_exact_time_slice(self):
        before_hash = hashlib.sha256(self.source.encode("utf-8")).hexdigest()
        linker = AsyncMock(return_value=[])
        with (
            patch.object(server, "bucket_mgr", self.manager),
            patch.object(server, "_auto_link_new_bucket", linker),
        ):
            preview = await server.split_bucket(
                self.bucket_id,
                start_time="2026-07-18T09:00",
                end_time="2026-07-18T09:00",
                new_name="第二段子桶",
            )
            count_after_preview = len(await self.manager.list_all(include_archive=True))
            result = await server.split_bucket(
                self.bucket_id,
                start_time="2026-07-18T09:00",
                end_time="2026-07-18T09:00",
                new_name="第二段子桶",
                confirm=True,
            )

        child_id = re.search(r"新 bucket_id: ([0-9a-f]+)", result).group(1)
        source_after = await self.manager.get(self.bucket_id)
        child = await self.manager.get(child_id)
        expected = self.source[
            self.source.index("--- 2026-07-18T09:00 ---") :
            self.source.index("--- 2026-07-19T09:00 ---")
        ]
        self.assertIn("【拆分演习】", preview)
        self.assertEqual(count_after_preview, 1)
        self.assertEqual(source_after["content"], self.source)
        self.assertEqual(
            hashlib.sha256(source_after["content"].encode("utf-8")).hexdigest(),
            before_hash,
        )
        self.assertEqual(child["content"], expected)
        linker.assert_awaited_once_with(child_id)

    async def test_marker_mode_copies_from_start_marker_without_touching_source(self):
        with (
            patch.object(server, "bucket_mgr", self.manager),
            patch.object(server, "_auto_link_new_bucket", new=AsyncMock(return_value=[])),
        ):
            result = await server.split_bucket(
                self.bucket_id,
                start_marker="第二段",
                end_marker="第三段",
                new_name="标记子桶",
                confirm=True,
            )

        child_id = re.search(r"新 bucket_id: ([0-9a-f]+)", result).group(1)
        child = await self.manager.get(child_id)
        self.assertEqual(child["content"], "第二段\n\n--- 2026-07-19T09:00 ---\n")
        self.assertEqual((await self.manager.get(self.bucket_id))["content"], self.source)


if __name__ == "__main__":
    unittest.main()
