import inspect
import os
import tempfile
import unittest
from unittest.mock import patch

import server
from bucket_manager import BucketManager


def make_config(root: str) -> dict:
    return {
        "buckets_dir": root,
        "embeddings": {"enabled": False},
        "history": {"db_path": os.path.join(root, "history.sqlite3")},
        "wikilink": {"enabled": False},
        "matching": {"fuzzy_threshold": 50, "max_results": 5},
    }


class FailingHistoryStore:
    async def snapshot(self, _bucket, _operation_type):
        raise OSError("snapshot unavailable")


class HistorySnapshotTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.manager = BucketManager(make_config(self.temp_dir.name))

    async def asyncTearDown(self):
        self.temp_dir.cleanup()

    async def test_replace_and_append_keep_complete_previous_versions(self):
        bucket_id = await self.manager.create(
            content="第一版完整正文",
            name="snapshot-test",
            tags=["原标签"],
            domain=["测试"],
            importance=7,
        )

        replaced = await self.manager.update(
            bucket_id,
            content="第二版替换正文",
            _history_operation="content_replace",
        )
        self.assertTrue(replaced)

        with (
            patch.object(server, "bucket_mgr", self.manager),
            patch.object(server, "_append_timestamp", return_value="2026-07-20T09:20"),
        ):
            response = await server.trace(
                bucket_id=bucket_id,
                content="第三段追加正文",
                append=True,
            )

        current = await self.manager.get(bucket_id)
        history = await self.manager.get_history(bucket_id, 10)

        self.assertIn("content=已追加", response)
        self.assertEqual(
            current["content"],
            "第二版替换正文\n\n--- 2026-07-20T09:20 ---\n第三段追加正文",
        )
        self.assertEqual(len(history), 2)
        self.assertEqual(history[0]["operation_type"], "content_append")
        self.assertEqual(history[0]["content"], "第二版替换正文")
        self.assertEqual(history[1]["operation_type"], "content_replace")
        self.assertEqual(history[1]["content"], "第一版完整正文")
        self.assertEqual(history[1]["metadata"]["tags"], ["原标签"])
        self.assertEqual(history[1]["metadata"]["importance"], 7)

    async def test_delete_is_snapshotted_and_history_remains_queryable(self):
        bucket_id = await self.manager.create(
            content="删除前完整正文",
            name="delete-snapshot-test",
            domain=["测试"],
        )

        self.assertTrue(await self.manager.delete(bucket_id))
        self.assertIsNone(await self.manager.get(bucket_id))
        history = await self.manager.get_history(bucket_id, 10)
        self.assertEqual(len(history), 1)
        self.assertEqual(history[0]["operation_type"], "delete")
        self.assertEqual(history[0]["content"], "删除前完整正文")

        with patch.object(server, "bucket_mgr", self.manager):
            response = await server.trace(bucket_id=bucket_id, history=True)
        self.assertIn("操作类型: delete", response)
        self.assertIn("删除前完整正文", response)

    async def test_snapshot_failure_blocks_update_and_delete(self):
        bucket_id = await self.manager.create(
            content="必须保留的原文",
            name="blocked-write-test",
            domain=["测试"],
        )
        self.manager.history_store = FailingHistoryStore()

        self.assertFalse(await self.manager.update(bucket_id, content="危险替换"))
        self.assertEqual((await self.manager.get(bucket_id))["content"], "必须保留的原文")
        self.assertFalse(await self.manager.delete(bucket_id))
        self.assertIsNotNone(await self.manager.get(bucket_id))

    async def test_shorter_write_is_rejected_before_snapshot(self):
        bucket_id = await self.manager.create(
            content="这是一段绝对不能缩水的完整正文",
            name="shortening-guard-test",
            domain=["测试"],
        )
        before_history = await self.manager.get_history(bucket_id, 10)

        self.assertFalse(await self.manager.update(bucket_id, content="太短"))

        current = await self.manager.get(bucket_id)
        after_history = await self.manager.get_history(bucket_id, 10)
        self.assertEqual(current["content"], "这是一段绝对不能缩水的完整正文")
        self.assertEqual(after_history, before_history)

    async def test_metadata_only_update_keeps_complete_previous_version(self):
        bucket_id = await self.manager.create(
            content="正文不变",
            name="metadata-only-test",
            domain=["测试"],
        )
        self.assertTrue(
            await self.manager.update(
                bucket_id,
                importance=8,
                _history_operation="metadata_update",
            )
        )
        history = await self.manager.get_history(bucket_id, 10)
        self.assertEqual(len(history), 1)
        self.assertEqual(history[0]["operation_type"], "metadata_update")
        self.assertEqual(history[0]["content"], "正文不变")
        self.assertEqual(history[0]["metadata"]["importance"], 5)

    async def test_trace_writes_sort_order_without_changing_content(self):
        bucket_id = await self.manager.create(
            content="排序时正文必须保持原样",
            name="sort-order-test",
            pinned=True,
        )

        with patch.object(server, "bucket_mgr", self.manager):
            response = await server.trace(bucket_id=bucket_id, sort_order=10000)

        current = await self.manager.get(bucket_id)
        history = await self.manager.get_history(bucket_id, 10)
        self.assertIn("sort_order=10000", response)
        self.assertEqual(current["metadata"]["sort_order"], 10000)
        self.assertEqual(current["content"], "排序时正文必须保持原样")
        self.assertEqual(len(history), 1)
        self.assertEqual(history[0]["operation_type"], "metadata_update")
        self.assertNotIn("sort_order", history[0]["metadata"])
        self.assertEqual(history[0]["content"], "排序时正文必须保持原样")

    def test_trace_keeps_old_parameters_and_appends_new_modes(self):
        parameters = list(inspect.signature(server.trace).parameters)
        self.assertEqual(
            parameters[:10],
            [
                "bucket_id",
                "name",
                "domain",
                "valence",
                "arousal",
                "importance",
                "tags",
                "resolved",
                "pinned",
                "delete",
            ],
        )
        self.assertEqual(
            parameters[10:],
            [
                "content",
                "append",
                "history",
                "history_limit",
                "sealed",
                "feeling",
                "trigger_date",
                "trigger_processed",
                "pin_level",
                "confirm_pin_level",
                "sort_order",
            ],
        )


if __name__ == "__main__":
    unittest.main()
