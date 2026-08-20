import sqlite3
import tempfile
import unittest
from pathlib import Path

from vault_health import VaultHealthCheck


class FakeEmbeddingIndex:
    enabled = True

    @staticmethod
    def pending_count():
        return 1

    @staticmethod
    def count():
        return 2


class VaultHealthTests(unittest.TestCase):
    def test_read_only_health_report_checks_markdown_sqlite_and_queue(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            memory_dir = root / "permanent" / "测试"
            memory_dir.mkdir(parents=True)
            memory = memory_dir / "一条记忆_bucket-1.md"
            original = "---\nid: bucket-1\nname: 一条记忆\n---\n正文保持不变\n"
            memory.write_text(original, encoding="utf-8")
            database = root / "history.sqlite3"
            connection = sqlite3.connect(database)
            try:
                connection.execute("CREATE TABLE history (id INTEGER PRIMARY KEY)")
                connection.commit()
            finally:
                connection.close()

            result = VaultHealthCheck(root, FakeEmbeddingIndex()).run()

            self.assertEqual(memory.read_text(encoding="utf-8"), original)
            self.assertEqual(result["status"], "warning")
            self.assertEqual(result["memory"]["files"], 1)
            self.assertEqual(result["database_count"], 1)
            self.assertEqual(result["embedding_count"], 2)
            self.assertEqual(result["embedding_queue"], 1)
            self.assertEqual(len(result["memory"]["fingerprint_sha256"]), 64)

    def test_duplicate_bucket_ids_are_reported(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            memory_dir = root / "dynamic" / "测试"
            memory_dir.mkdir(parents=True)
            text = "---\nid: repeated\nname: 重复\n---\n正文\n"
            (memory_dir / "a.md").write_text(text, encoding="utf-8")
            (memory_dir / "b.md").write_text(text, encoding="utf-8")

            result = VaultHealthCheck(root).run()

            self.assertEqual(result["status"], "error")
            self.assertTrue(
                any(item["kind"] == "duplicate_id" for item in result["issues"])
            )
