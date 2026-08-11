"""SQLite sidecar for semantic links between memory buckets."""

import asyncio
import os
import sqlite3
from pathlib import Path

from utils import now_iso


class RelationStore:
    """Store links outside Markdown so bucket text and metadata stay untouched."""

    def __init__(self, config: dict):
        settings = config.get("relations", {})
        self.enabled = bool(settings.get("enabled", True))
        self.similarity_threshold = max(
            0.0,
            min(1.0, float(settings.get("similarity_threshold", 0.86))),
        )
        self.max_links = max(1, int(settings.get("max_links", 3)))
        self.db_path = settings.get("db_path") or os.environ.get(
            "OMBRE_RELATIONS_DB",
            os.path.join(config["buckets_dir"], "relations.sqlite3"),
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
                CREATE TABLE IF NOT EXISTS relations (
                    left_bucket_id TEXT NOT NULL,
                    right_bucket_id TEXT NOT NULL,
                    similarity REAL NOT NULL,
                    model TEXT NOT NULL,
                    source TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY (left_bucket_id, right_bucket_id),
                    CHECK (left_bucket_id < right_bucket_id)
                )
                """
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_relations_left "
                "ON relations(left_bucket_id, similarity DESC)"
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_relations_right "
                "ON relations(right_bucket_id, similarity DESC)"
            )

    def _upsert_sync(
        self,
        bucket_id: str,
        neighbors: list[tuple[str, float]],
        model: str,
    ) -> int:
        now = now_iso()
        rows = []
        for other_id, similarity in neighbors:
            if not other_id or other_id == bucket_id:
                continue
            left_id, right_id = sorted((bucket_id, other_id))
            rows.append(
                (
                    left_id,
                    right_id,
                    float(similarity),
                    model,
                    "auto_new_bucket",
                    now,
                )
            )
        if not rows:
            return 0
        with self._connect() as connection:
            connection.executemany(
                """
                INSERT INTO relations
                    (left_bucket_id, right_bucket_id, similarity, model, source, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(left_bucket_id, right_bucket_id) DO UPDATE SET
                    similarity=excluded.similarity,
                    model=excluded.model,
                    source=excluded.source,
                    created_at=excluded.created_at
                """,
                rows,
            )
        return len(rows)

    async def upsert_new_bucket_links(
        self,
        bucket_id: str,
        neighbors: list[tuple[str, float]],
        model: str,
    ) -> int:
        if not self.enabled:
            return 0
        return await asyncio.to_thread(
            self._upsert_sync, bucket_id, neighbors, model
        )

    def _related_sync(self, bucket_id: str) -> list[tuple[str, float]]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT left_bucket_id, right_bucket_id, similarity
                FROM relations
                WHERE left_bucket_id=? OR right_bucket_id=?
                ORDER BY similarity DESC, left_bucket_id, right_bucket_id
                """,
                (bucket_id, bucket_id),
            ).fetchall()
        return [
            (
                row["right_bucket_id"]
                if row["left_bucket_id"] == bucket_id
                else row["left_bucket_id"],
                float(row["similarity"]),
            )
            for row in rows
        ]

    async def related(self, bucket_id: str) -> list[tuple[str, float]]:
        if not self.enabled:
            return []
        return await asyncio.to_thread(self._related_sync, bucket_id)

    def count(self) -> int:
        if not self.enabled:
            return 0
        with self._connect() as connection:
            return int(connection.execute("SELECT COUNT(*) FROM relations").fetchone()[0])
