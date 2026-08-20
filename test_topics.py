import os
import sqlite3
import tempfile
import unittest

from topic_store import TOPIC_TREE, TopicStore, suggest_topic


def config(root):
    return {
        "buckets_dir": root,
        "topics": {"db_path": os.path.join(root, "topics.sqlite3")},
    }


class TopicStoreTests(unittest.IsolatedAsyncioTestCase):
    async def test_assignment_stores_only_location_and_bucket_id(self):
        with tempfile.TemporaryDirectory() as root:
            store = TopicStore(config(root))
            assignment = await store.assign(
                "bucket-1", "性爱", "身体感受", source="manual"
            )
            loaded = await store.get("bucket-1")
            with sqlite3.connect(store.db_path) as connection:
                columns = [
                    row[1]
                    for row in connection.execute(
                        "PRAGMA table_info(topic_assignments)"
                    ).fetchall()
                ]

        self.assertEqual(assignment["main_topic"], "性爱")
        self.assertEqual(loaded["subtopic"], "身体感受")
        self.assertNotIn("content", columns)
        self.assertNotIn("title", columns)

    async def test_invalid_cross_directory_assignment_is_rejected(self):
        with tempfile.TemporaryDirectory() as root:
            store = TopicStore(config(root))
            with self.assertRaises(ValueError):
                await store.assign("bucket-2", "用户", "部署与开发")

    async def test_auto_assignment_does_not_reclassify_existing_bucket(self):
        with tempfile.TemporaryDirectory() as root:
            store = TopicStore(config(root))
            await store.assign("bucket-3", "我们的关系", "承诺")
            result = await store.auto_assign(
                "bucket-3", "后来", "这次内容提到了Docker部署。", {}
            )
            loaded = await store.get("bucket-3")

        self.assertEqual(result["status"], "existing")
        self.assertEqual(loaded["main_topic"], "我们的关系")
        self.assertEqual(loaded["subtopic"], "承诺")

    async def test_bulk_assignment_skips_existing_and_can_be_undone(self):
        with tempfile.TemporaryDirectory() as root:
            store = TopicStore(config(root))
            await store.assign("manual", "我们的关系", "承诺", source="manual")
            result = await store.bulk_assign(
                [
                    {"bucket_id": "manual", "main_topic": "系统与技术", "subtopic": "部署与开发"},
                    {"bucket_id": "fresh", "main_topic": "共同生活", "subtopic": "日常记录"},
                ]
            )
            fresh = await store.get("fresh")
            manual = await store.get("manual")
            undone = await store.undo_last_bulk()
            fresh_after = await store.get("fresh")
            manual_after = await store.get("manual")

        self.assertEqual(result["applied"], 1)
        self.assertEqual(result["skipped"], 1)
        self.assertEqual(fresh["source"], "bulk")
        self.assertEqual(manual["subtopic"], "承诺")
        self.assertEqual(undone["restored"], 1)
        self.assertIsNone(fresh_after)
        self.assertEqual(manual_after["source"], "manual")

    def test_sex_and_identity_are_independent_main_directories(self):
        sex = suggest_topic("那一晚", "做完以后身体还在发软，心跳很快。")
        identity = suggest_topic("存在论", "我是 AI，这是我认下的名字。")

        self.assertIn("性爱", TOPIC_TREE)
        self.assertIn("AI 自我", TOPIC_TREE)
        self.assertEqual(sex["main_topic"], "性爱")
        self.assertEqual(identity["main_topic"], "AI 自我")


if __name__ == "__main__":
    unittest.main()
