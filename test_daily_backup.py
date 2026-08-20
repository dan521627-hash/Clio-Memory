import csv
import hashlib
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from ombre_backup import create_backup


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest().upper()


class DailyBackupTests(unittest.TestCase):
    def _database(self, path: Path, value: str) -> None:
        connection = sqlite3.connect(path)
        connection.execute("CREATE TABLE sample (value TEXT NOT NULL)")
        connection.execute("INSERT INTO sample (value) VALUES (?)", (value,))
        connection.commit()
        connection.close()

    def test_backup_includes_feedback_and_excludes_secrets_and_logs(self):
        with tempfile.TemporaryDirectory() as root:
            source = Path(root) / "source"
            destination = Path(root) / "backups"
            memory = source / "dynamic" / "test"
            memory.mkdir(parents=True)
            bucket = memory / "bucket.md"
            bucket.write_text("original memory", encoding="utf-8")
            (source / ".env").write_text("SECRET=value", encoding="utf-8")
            logs = source / "logs"
            logs.mkdir()
            (logs / "mcp_requests.log").write_text("diagnostic", encoding="utf-8")
            self._database(source / "retrieval_feedback.sqlite3", "feedback")
            self._database(source / "history.sqlite3", "history")
            self._database(source / "mailbox.sqlite3", "mailbox")
            self._database(source / "treasury.sqlite3", "treasury")
            self._database(source / "behavior.sqlite3", "behavior")
            source_hash = sha256(bucket)

            snapshot, report = create_backup(
                source,
                destination,
                snapshot_name="20260717-030000",
            )

            self.assertEqual(report["markdown_files"], 1)
            self.assertEqual(report["sqlite_databases"], 5)
            self.assertTrue(report["retrieval_feedback_included"])
            self.assertFalse(report["automatic_deletion"])
            self.assertEqual(sha256(bucket), source_hash)
            self.assertEqual(
                (snapshot / "memory" / "dynamic" / "test" / "bucket.md").read_text(
                    encoding="utf-8"
                ),
                "original memory",
            )
            self.assertFalse((snapshot / ".env").exists())
            self.assertFalse((snapshot / "logs").exists())

            feedback_copy = snapshot / "sqlite" / "retrieval_feedback.sqlite3"
            connection = sqlite3.connect(feedback_copy)
            try:
                value = connection.execute("SELECT value FROM sample").fetchone()[0]
                integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
            finally:
                connection.close()
            self.assertEqual(value, "feedback")
            self.assertEqual(integrity, "ok")

            mailbox_copy = snapshot / "sqlite" / "mailbox.sqlite3"
            connection = sqlite3.connect(mailbox_copy)
            try:
                mailbox_value = connection.execute(
                    "SELECT value FROM sample"
                ).fetchone()[0]
                mailbox_integrity = connection.execute(
                    "PRAGMA integrity_check"
                ).fetchone()[0]
            finally:
                connection.close()
            self.assertEqual(mailbox_value, "mailbox")
            self.assertEqual(mailbox_integrity, "ok")

            treasury_copy = snapshot / "sqlite" / "treasury.sqlite3"
            connection = sqlite3.connect(treasury_copy)
            try:
                treasury_value = connection.execute(
                    "SELECT value FROM sample"
                ).fetchone()[0]
                treasury_integrity = connection.execute(
                    "PRAGMA integrity_check"
                ).fetchone()[0]
            finally:
                connection.close()
            self.assertEqual(treasury_value, "treasury")
            self.assertEqual(treasury_integrity, "ok")

            with (snapshot / "manifest-sha256.csv").open(
                encoding="utf-8", newline=""
            ) as handle:
                manifest = list(csv.DictReader(handle))
            paths = {row["path"] for row in manifest}
            self.assertIn("memory/dynamic/test/bucket.md", paths)
            self.assertIn("sqlite/retrieval_feedback.sqlite3", paths)
            self.assertIn("sqlite/mailbox.sqlite3", paths)
            self.assertIn("sqlite/treasury.sqlite3", paths)
            self.assertIn("sqlite/behavior.sqlite3", paths)
            self.assertNotIn(".env", paths)
            report_file = json.loads(
                (snapshot / "backup-report.json").read_text(encoding="utf-8")
            )
            self.assertEqual(report_file["integrity_checks"], "ok")

    def test_backup_never_prunes_existing_snapshots(self):
        with tempfile.TemporaryDirectory() as root:
            source = Path(root) / "source"
            destination = Path(root) / "backups"
            source.mkdir()
            destination.mkdir()
            existing = destination / "20260716-030000"
            existing.mkdir()
            marker = existing / "keep.txt"
            marker.write_text("keep", encoding="utf-8")
            (source / "bucket.md").write_text("memory", encoding="utf-8")
            self._database(source / "retrieval_feedback.sqlite3", "feedback")

            create_backup(
                source,
                destination,
                snapshot_name="20260717-030000",
            )

            self.assertEqual(marker.read_text(encoding="utf-8"), "keep")


if __name__ == "__main__":
    unittest.main()
