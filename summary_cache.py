"""SQLite cache for reusable memory summaries."""

import asyncio
import hashlib
import os
import sqlite3
from pathlib import Path

from utils import now_iso


class SummaryCache:
    """Cache summaries by bucket and content fingerprint, never full source text."""

    def __init__(self, config: dict):
        settings = config.get("summary_cache", {})
        self.enabled = bool(settings.get("enabled", True))
        buckets_dir = config.get("buckets_dir") or os.path.join(
            os.path.dirname(os.path.abspath(__file__)), "buckets"
        )
        self.db_path = settings.get("db_path") or os.environ.get(
            "OMBRE_SUMMARY_CACHE_DB",
            os.path.join(buckets_dir, "summaries.sqlite3"),
        )
        if self.enabled:
            self._init_db()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.db_path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA synchronous=NORMAL")
        return connection

    def _init_db(self) -> None:
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS summary_cache (
                    bucket_id TEXT PRIMARY KEY,
                    content_hash TEXT NOT NULL,
                    model TEXT NOT NULL,
                    prompt_hash TEXT NOT NULL,
                    summary_text TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_summary_cache_fingerprint "
                "ON summary_cache(content_hash, model, prompt_hash)"
            )

    @staticmethod
    def fingerprint(content: str) -> str:
        return hashlib.sha256(str(content).encode("utf-8")).hexdigest()

    def _get_sync(
        self,
        bucket_id: str,
        content_hash: str,
        model: str,
        prompt_hash: str,
    ) -> str | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT summary_text FROM summary_cache
                WHERE bucket_id=? AND content_hash=? AND model=? AND prompt_hash=?
                """,
                (bucket_id, content_hash, model, prompt_hash),
            ).fetchone()
        return str(row["summary_text"]) if row else None

    async def get(
        self,
        bucket_id: str,
        content_hash: str,
        model: str,
        prompt_hash: str,
    ) -> str | None:
        if not self.enabled or not bucket_id:
            return None
        return await asyncio.to_thread(
            self._get_sync, bucket_id, content_hash, model, prompt_hash
        )

    def _put_sync(
        self,
        bucket_id: str,
        content_hash: str,
        model: str,
        prompt_hash: str,
        summary_text: str,
    ) -> None:
        now = now_iso()
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO summary_cache (
                    bucket_id, content_hash, model, prompt_hash,
                    summary_text, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(bucket_id) DO UPDATE SET
                    content_hash=excluded.content_hash,
                    model=excluded.model,
                    prompt_hash=excluded.prompt_hash,
                    summary_text=excluded.summary_text,
                    updated_at=excluded.updated_at
                """,
                (
                    bucket_id,
                    content_hash,
                    model,
                    prompt_hash,
                    summary_text,
                    now,
                    now,
                ),
            )

    async def put(
        self,
        bucket_id: str,
        content_hash: str,
        model: str,
        prompt_hash: str,
        summary_text: str,
    ) -> None:
        if not self.enabled or not bucket_id or not summary_text:
            return
        await asyncio.to_thread(
            self._put_sync,
            bucket_id,
            content_hash,
            model,
            prompt_hash,
            summary_text,
        )

    def _delete_sync(self, bucket_id: str) -> None:
        with self._connect() as connection:
            connection.execute(
                "DELETE FROM summary_cache WHERE bucket_id=?", (bucket_id,)
            )

    async def delete(self, bucket_id: str) -> None:
        if self.enabled and bucket_id:
            await asyncio.to_thread(self._delete_sync, bucket_id)

    def count(self) -> int:
        if not self.enabled:
            return 0
        with self._connect() as connection:
            return int(
                connection.execute("SELECT COUNT(*) FROM summary_cache").fetchone()[0]
            )
