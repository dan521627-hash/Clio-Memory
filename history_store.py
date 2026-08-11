import asyncio
import json
import os
import sqlite3
from datetime import datetime, timedelta, timezone

from utils import beijing_now, now_iso


class HistoryStore:
    def __init__(self, config: dict):
        settings = config.get("history", {})
        self.db_path = settings.get("db_path") or os.path.join(
            config["buckets_dir"], "history.sqlite3"
        )
        os.makedirs(os.path.dirname(os.path.abspath(self.db_path)), exist_ok=True)
        self._initialize()

    def _connect(self):
        connection = sqlite3.connect(self.db_path, timeout=30)
        connection.row_factory = sqlite3.Row
        return connection

    def _initialize(self):
        with self._connect() as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS bucket_history (
                    snapshot_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    bucket_id TEXT NOT NULL,
                    snapshot_at TEXT NOT NULL,
                    operation_type TEXT NOT NULL,
                    content TEXT NOT NULL,
                    metadata_json TEXT NOT NULL,
                    source_path TEXT NOT NULL DEFAULT ''
                )
                """
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_bucket_history_bucket_time "
                "ON bucket_history(bucket_id, snapshot_id DESC)"
            )

    def _snapshot_sync(self, bucket: dict, operation_type: str) -> int:
        metadata = bucket.get("metadata", {})
        with self._connect() as connection:
            cursor = connection.execute(
                """
                INSERT INTO bucket_history (
                    bucket_id, snapshot_at, operation_type,
                    content, metadata_json, source_path
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    bucket["id"],
                    now_iso(),
                    operation_type,
                    bucket.get("content", ""),
                    json.dumps(metadata, ensure_ascii=False, default=str),
                    bucket.get("path", ""),
                ),
            )
            return int(cursor.lastrowid)

    async def snapshot(self, bucket: dict, operation_type: str) -> int:
        return await asyncio.to_thread(self._snapshot_sync, bucket, operation_type)

    def _list_sync(self, bucket_id: str, limit: int) -> list[dict]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT snapshot_id, bucket_id, snapshot_at, operation_type,
                       content, metadata_json, source_path
                FROM bucket_history
                WHERE bucket_id=?
                ORDER BY snapshot_id DESC
                LIMIT ?
                """,
                (bucket_id, limit),
            ).fetchall()

        snapshots = []
        for row in rows:
            try:
                metadata = json.loads(row["metadata_json"])
            except json.JSONDecodeError:
                metadata = {"_raw": row["metadata_json"]}
            snapshots.append(
                {
                    "snapshot_id": row["snapshot_id"],
                    "bucket_id": row["bucket_id"],
                    "snapshot_at": row["snapshot_at"],
                    "operation_type": row["operation_type"],
                    "content": row["content"],
                    "metadata": metadata,
                    "source_path": row["source_path"],
                }
            )
        return snapshots

    async def list(self, bucket_id: str, limit: int = 10) -> list[dict]:
        safe_limit = max(1, min(50, int(limit)))
        return await asyncio.to_thread(self._list_sync, bucket_id, safe_limit)

    def _preview_retention_sync(
        self,
        *,
        retention_days: int,
        min_versions_per_bucket: int,
        protected_bucket_ids: set[str],
        protect_delete_snapshots: bool,
        protect_important_snapshots: bool,
        now: datetime,
    ) -> dict:
        reference = now.astimezone(timezone.utc)
        cutoff = reference - timedelta(days=retention_days)
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT snapshot_id, bucket_id, snapshot_at,
                       operation_type, metadata_json
                FROM bucket_history
                ORDER BY bucket_id, snapshot_id DESC
                """
            ).fetchall()

        candidates = []
        ranks: dict[str, int] = {}
        protected_counts = {
            "too_recent": 0,
            "minimum_versions": 0,
            "delete_snapshot": 0,
            "important_memory": 0,
            "invalid_timestamp": 0,
        }
        for row in rows:
            bucket_id = row["bucket_id"]
            rank = ranks.get(bucket_id, 0) + 1
            ranks[bucket_id] = rank
            try:
                snapshot_at = datetime.fromisoformat(
                    str(row["snapshot_at"]).replace("Z", "+00:00")
                )
                if snapshot_at.tzinfo is None:
                    snapshot_at = snapshot_at.replace(tzinfo=timezone.utc)
                else:
                    snapshot_at = snapshot_at.astimezone(timezone.utc)
            except ValueError:
                protected_counts["invalid_timestamp"] += 1
                continue
            if snapshot_at >= cutoff:
                protected_counts["too_recent"] += 1
                continue
            if rank <= min_versions_per_bucket:
                protected_counts["minimum_versions"] += 1
                continue
            if protect_delete_snapshots and row["operation_type"] == "delete":
                protected_counts["delete_snapshot"] += 1
                continue
            try:
                metadata = json.loads(row["metadata_json"])
            except json.JSONDecodeError:
                metadata = {}
            important_snapshot = any(
                metadata.get(key, False) for key in ("pinned", "protected", "sealed")
            )
            if protect_important_snapshots and (
                important_snapshot or bucket_id in protected_bucket_ids
            ):
                protected_counts["important_memory"] += 1
                continue
            candidates.append(
                {
                    "snapshot_id": int(row["snapshot_id"]),
                    "bucket_id": bucket_id,
                    "snapshot_at": row["snapshot_at"],
                    "operation_type": row["operation_type"],
                }
            )

        return {
            "mode": "report_only",
            "checked": len(rows),
            "candidate_count": len(candidates),
            "candidates": candidates,
            "retention_days": retention_days,
            "min_versions_per_bucket": min_versions_per_bucket,
            "cutoff": cutoff.isoformat(),
            "protected_counts": protected_counts,
        }

    async def preview_retention(
        self,
        *,
        retention_days: int,
        min_versions_per_bucket: int,
        protected_bucket_ids: set[str] | None = None,
        protect_delete_snapshots: bool = True,
        protect_important_snapshots: bool = True,
        now: datetime | None = None,
    ) -> dict:
        reference = beijing_now(now)
        return await asyncio.to_thread(
            self._preview_retention_sync,
            retention_days=max(1, int(retention_days)),
            min_versions_per_bucket=max(1, int(min_versions_per_bucket)),
            protected_bucket_ids=set(protected_bucket_ids or set()),
            protect_delete_snapshots=bool(protect_delete_snapshots),
            protect_important_snapshots=bool(protect_important_snapshots),
            now=reference,
        )

    def count(self, bucket_id: str = "") -> int:
        with self._connect() as connection:
            if bucket_id:
                row = connection.execute(
                    "SELECT COUNT(*) FROM bucket_history WHERE bucket_id=?",
                    (bucket_id,),
                ).fetchone()
            else:
                row = connection.execute("SELECT COUNT(*) FROM bucket_history").fetchone()
        return int(row[0])
