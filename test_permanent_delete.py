import asyncio
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock

from bucket_manager import BucketManager
from permanent_delete import PermanentDeleteService


class PermanentDeleteServiceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.config = {
            "buckets_dir": str(self.root),
            "history": {"db_path": str(self.root / "history.sqlite3")},
            "embeddings": {"db_path": str(self.root / "embeddings.sqlite3")},
            "summary_cache": {"db_path": str(self.root / "summaries.sqlite3")},
            "relations": {"db_path": str(self.root / "relations.sqlite3")},
            "retrieval_feedback": {"db_path": str(self.root / "retrieval_feedback.sqlite3")},
            "fact_timeline": {"db_path": str(self.root / "fact_timeline.sqlite3")},
            "topics": {"db_path": str(self.root / "topics.sqlite3")},
            "xinchao": {"db_path": str(self.root / "xinchao.sqlite3")},
            "behavior": {"db_path": str(self.root / "behavior.sqlite3")},
        }
        self.target = "abc123def456"
        self.other = "fff111eee222"

    def tearDown(self):
        self.temp.cleanup()

    def _database(self, name: str, statements: list[str], inserts: list[tuple[str, tuple]]):
        with sqlite3.connect(self.root / name) as connection:
            for statement in statements:
                connection.execute(statement)
            for statement, values in inserts:
                connection.execute(statement, values)

    def test_purge_removes_only_rows_linked_to_target_bucket(self):
        self._database(
            "history.sqlite3",
            ["CREATE TABLE bucket_history(bucket_id TEXT)"],
            [
                ("INSERT INTO bucket_history VALUES (?)", (self.target,)),
                ("INSERT INTO bucket_history VALUES (?)", (self.other,)),
            ],
        )
        self._database(
            "relations.sqlite3",
            ["CREATE TABLE relations(left_bucket_id TEXT, right_bucket_id TEXT)"],
            [
                ("INSERT INTO relations VALUES (?, ?)", (self.target, self.other)),
                ("INSERT INTO relations VALUES (?, ?)", ("aaa", "bbb")),
            ],
        )
        self._database(
            "summaries.sqlite3",
            ["CREATE TABLE summary_cache(bucket_id TEXT)"],
            [
                ("INSERT INTO summary_cache VALUES (?)", (self.target,)),
                ("INSERT INTO summary_cache VALUES (?)", (self.other,)),
            ],
        )
        self._database(
            "xinchao.sqlite3",
            [
                "CREATE TABLE xinchao_events(source_ref TEXT)",
                "CREATE TABLE xinchao_darkflow(context_json TEXT)",
                "CREATE TABLE xinchao_transitions(details_json TEXT)",
            ],
            [
                ("INSERT INTO xinchao_events VALUES (?)", (self.target,)),
                ("INSERT INTO xinchao_events VALUES (?)", (self.other,)),
                ("INSERT INTO xinchao_darkflow VALUES (?)", (f'{{\"bucket_id\":\"{self.target}\"}}',)),
                ("INSERT INTO xinchao_darkflow VALUES (?)", ('{}',)),
            ],
        )
        service = PermanentDeleteService(self.config)
        preview = asyncio.run(service.preview(self.target))
        self.assertEqual(preview["history_snapshots"], 1)
        self.assertEqual(preview["relations"], 1)
        self.assertEqual(preview["summary"], 1)
        self.assertEqual(preview["emotion_events"], 1)
        self.assertEqual(preview["darkflow_context"], 1)

        removed = asyncio.run(service.purge(self.target))
        self.assertEqual(removed["history_snapshots"], 1)
        self.assertEqual(removed["relations"], 1)
        self.assertEqual(removed["summary"], 1)
        self.assertEqual(removed["emotion_events"], 1)
        self.assertEqual(removed["darkflow_context"], 1)

        with sqlite3.connect(self.root / "history.sqlite3") as connection:
            self.assertEqual(connection.execute("SELECT bucket_id FROM bucket_history").fetchall(), [(self.other,)])
        with sqlite3.connect(self.root / "relations.sqlite3") as connection:
            self.assertEqual(connection.execute("SELECT * FROM relations").fetchall(), [("aaa", "bbb")])


class PermanentBucketFileTests(unittest.IsolatedAsyncioTestCase):
    async def test_permanent_delete_skips_history_snapshot(self):
        with tempfile.TemporaryDirectory() as temp:
            bucket_file = Path(temp) / "test.md"
            bucket_file.write_text("original", encoding="utf-8")
            manager = BucketManager.__new__(BucketManager)
            manager._find_bucket_file = lambda _bucket_id: str(bucket_file)
            manager.embedding_index = AsyncMock()
            manager.history_store = AsyncMock()

            deleted = await manager.delete_permanently("abc123def456")

            self.assertTrue(deleted)
            self.assertFalse(bucket_file.exists())
            manager.embedding_index.delete.assert_awaited_once_with("abc123def456")
            manager.history_store.snapshot.assert_not_called()


if __name__ == "__main__":
    unittest.main()
