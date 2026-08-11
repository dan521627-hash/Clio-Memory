"""Independent SQLite ledger for unfinished matters."""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import sqlite3
from contextlib import contextmanager
from datetime import timedelta
from pathlib import Path

import numpy as np

from utils import beijing_now, now_iso


logger = logging.getLogger("ombre_brain.tasks")


class TaskStore:
    """Keep tasks separate from memory bucket bodies."""

    STATUSES = {"open", "completed", "cancelled"}

    def __init__(self, config: dict):
        settings = config.get("tasks", {})
        self.db_path = settings.get("db_path") or os.environ.get(
            "OMBRE_TASKS_DB",
            os.path.join(config["buckets_dir"], "tasks.sqlite3"),
        )
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    @contextmanager
    def _connect(self):
        connection = sqlite3.connect(self.db_path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA synchronous=NORMAL")
        connection.execute("PRAGMA foreign_keys=ON")
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS tasks (
                    task_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    title TEXT NOT NULL,
                    details TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL DEFAULT 'open',
                    importance INTEGER NOT NULL DEFAULT 3,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    completed_at TEXT,
                    created_by TEXT NOT NULL DEFAULT 'auto',
                    manual_updated_at TEXT,
                    completion_notice_pending INTEGER NOT NULL DEFAULT 0,
                    deleted_at TEXT,
                    embedding BLOB,
                    embedding_model TEXT,
                    embedding_dimensions INTEGER,
                    embedding_hash TEXT,
                    CHECK (status IN ('open', 'completed', 'cancelled')),
                    CHECK (importance BETWEEN 1 AND 5)
                );
                CREATE TABLE IF NOT EXISTS task_sources (
                    source_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    task_id INTEGER NOT NULL,
                    source_type TEXT NOT NULL,
                    source_ref TEXT NOT NULL DEFAULT '',
                    source_event_id TEXT NOT NULL DEFAULT '',
                    excerpt TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(task_id) REFERENCES tasks(task_id) ON DELETE CASCADE,
                    UNIQUE(task_id, source_type, source_ref, source_event_id)
                );
                CREATE TABLE IF NOT EXISTS task_events (
                    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_key TEXT NOT NULL UNIQUE,
                    content_hash TEXT NOT NULL,
                    source_type TEXT NOT NULL,
                    source_ref TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL,
                    result_json TEXT NOT NULL DEFAULT '',
                    error TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS task_history (
                    history_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    task_id INTEGER NOT NULL,
                    snapshot_at TEXT NOT NULL,
                    operation TEXT NOT NULL,
                    title TEXT NOT NULL,
                    details TEXT NOT NULL,
                    status TEXT NOT NULL,
                    importance INTEGER NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    completed_at TEXT,
                    created_by TEXT NOT NULL,
                    manual_updated_at TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_tasks_status_importance
                    ON tasks(deleted_at, status, importance DESC, updated_at DESC);
                CREATE INDEX IF NOT EXISTS idx_task_sources_task
                    ON task_sources(task_id, source_id DESC);
                CREATE INDEX IF NOT EXISTS idx_task_events_hash
                    ON task_events(content_hash, created_at DESC);
                CREATE INDEX IF NOT EXISTS idx_task_history_task
                    ON task_history(task_id, history_id DESC);
                """
            )

    @staticmethod
    def _clean_title(value: str) -> str:
        title = " ".join(str(value or "").strip().split())
        if not title:
            raise ValueError("未竟事项必须有标题。")
        if len(title) > 160:
            raise ValueError("标题不能超过 160 个字符。")
        return title

    @staticmethod
    def _clean_details(value: str) -> str:
        details = str(value or "").strip()
        if len(details) > 4000:
            raise ValueError("详细内容不能超过 4000 个字符。")
        return details

    @classmethod
    def _clean_status(cls, value: str) -> str:
        status = str(value or "open").strip().lower()
        aliases = {"未完成": "open", "完成": "completed", "取消": "cancelled"}
        status = aliases.get(status, status)
        if status not in cls.STATUSES:
            raise ValueError("状态只能是 open、completed 或 cancelled。")
        return status

    @staticmethod
    def _clean_importance(value: int) -> int:
        try:
            importance = int(value)
        except (TypeError, ValueError):
            raise ValueError("重要程度必须是 1 到 5 的整数。") from None
        if not 1 <= importance <= 5:
            raise ValueError("重要程度必须在 1 到 5 之间。")
        return importance

    @staticmethod
    def task_text(item: dict) -> str:
        return f"{item.get('title', '')}\n{item.get('details', '')}".strip()

    @staticmethod
    def content_hash(text: str) -> str:
        return hashlib.sha256(str(text).encode("utf-8")).hexdigest()

    @staticmethod
    def _row(row: sqlite3.Row | None) -> dict | None:
        return dict(row) if row is not None else None

    def _snapshot(self, connection: sqlite3.Connection, row: sqlite3.Row, operation: str) -> None:
        connection.execute(
            """
            INSERT INTO task_history (
                task_id, snapshot_at, operation, title, details, status,
                importance, created_at, updated_at, completed_at,
                created_by, manual_updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                row["task_id"], now_iso(), operation, row["title"], row["details"],
                row["status"], row["importance"], row["created_at"], row["updated_at"],
                row["completed_at"], row["created_by"], row["manual_updated_at"],
            ),
        )

    def _add_source(
        self,
        connection: sqlite3.Connection,
        task_id: int,
        source_type: str,
        source_ref: str,
        source_event_id: str,
        excerpt: str,
    ) -> None:
        connection.execute(
            """
            INSERT OR IGNORE INTO task_sources (
                task_id, source_type, source_ref, source_event_id, excerpt, created_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                task_id, str(source_type or "unknown")[:40], str(source_ref or "")[:160],
                str(source_event_id or "")[:240], str(excerpt or "").strip()[:800], now_iso(),
            ),
        )

    def _create_sync(
        self,
        title: str,
        details: str,
        importance: int,
        created_by: str,
        source_type: str,
        source_ref: str,
        source_event_id: str,
        excerpt: str,
    ) -> dict:
        now = now_iso()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            cursor = connection.execute(
                """
                INSERT INTO tasks (
                    title, details, status, importance, created_at, updated_at, created_by
                ) VALUES (?, ?, 'open', ?, ?, ?, ?)
                """,
                (
                    self._clean_title(title), self._clean_details(details),
                    self._clean_importance(importance), now, now,
                    str(created_by or "auto")[:40],
                ),
            )
            task_id = int(cursor.lastrowid)
            self._add_source(
                connection, task_id, source_type, source_ref, source_event_id, excerpt
            )
            row = connection.execute("SELECT * FROM tasks WHERE task_id=?", (task_id,)).fetchone()
        return self._row(row)

    async def create(self, **kwargs) -> dict:
        return await asyncio.to_thread(self._create_sync, **kwargs)

    def _get_sync(self, task_id: int, include_deleted: bool = False) -> dict | None:
        where = "task_id=?" if include_deleted else "task_id=? AND deleted_at IS NULL"
        with self._connect() as connection:
            row = connection.execute(f"SELECT * FROM tasks WHERE {where}", (int(task_id),)).fetchone()
            if row is None:
                return None
            item = dict(row)
            item["sources"] = [
                dict(source)
                for source in connection.execute(
                    "SELECT * FROM task_sources WHERE task_id=? ORDER BY source_id DESC",
                    (int(task_id),),
                ).fetchall()
            ]
            return item

    async def get(self, task_id: int, include_deleted: bool = False) -> dict | None:
        return await asyncio.to_thread(self._get_sync, task_id, include_deleted)

    def _list_sync(
        self,
        status: str = "",
        limit: int = 100,
        before_id: int = 0,
        include_deleted: bool = False,
    ) -> list[dict]:
        clauses = [] if include_deleted else ["deleted_at IS NULL"]
        params: list = []
        if status:
            clauses.append("status=?")
            params.append(self._clean_status(status))
        if before_id:
            clauses.append("task_id<?")
            params.append(int(before_id))
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        params.append(max(1, min(500, int(limit))))
        with self._connect() as connection:
            rows = connection.execute(
                f"SELECT * FROM tasks {where} "
                "ORDER BY CASE status WHEN 'open' THEN 0 WHEN 'completed' THEN 1 ELSE 2 END, "
                "importance DESC, updated_at DESC, task_id DESC LIMIT ?",
                params,
            ).fetchall()
        return [dict(row) for row in rows]

    async def list(self, **kwargs) -> list[dict]:
        return await asyncio.to_thread(self._list_sync, **kwargs)

    def _update_sync(
        self,
        task_id: int,
        *,
        title: str | None = None,
        details: str | None = None,
        status: str | None = None,
        importance: int | None = None,
        manual: bool = False,
        source_type: str = "",
        source_ref: str = "",
        source_event_id: str = "",
        excerpt: str = "",
    ) -> dict:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM tasks WHERE task_id=? AND deleted_at IS NULL",
                (int(task_id),),
            ).fetchone()
            if row is None:
                raise ValueError(f"找不到未竟事项 #{task_id}。")
            has_field_change = any(
                value is not None for value in (title, details, status, importance)
            )
            if not has_field_change and not manual:
                if source_type:
                    self._add_source(
                        connection, int(task_id), source_type, source_ref,
                        source_event_id, excerpt,
                    )
                return dict(row)
            self._snapshot(connection, row, "manual_update" if manual else "auto_update")
            new_title = self._clean_title(title) if title is not None else row["title"]
            new_details = self._clean_details(details) if details is not None else row["details"]
            new_status = self._clean_status(status) if status is not None else row["status"]
            new_importance = self._clean_importance(importance) if importance is not None else row["importance"]
            now = now_iso()
            completed_at = row["completed_at"]
            pending = int(row["completion_notice_pending"] or 0)
            if new_status == "completed" and row["status"] != "completed":
                completed_at = now
                pending = 1
            elif new_status != "completed":
                completed_at = None
                pending = 0
            manual_at = now if manual else row["manual_updated_at"]
            connection.execute(
                """
                UPDATE tasks SET title=?, details=?, status=?, importance=?, updated_at=?,
                    completed_at=?, manual_updated_at=?, completion_notice_pending=?
                WHERE task_id=?
                """,
                (
                    new_title, new_details, new_status, new_importance, now,
                    completed_at, manual_at, pending, int(task_id),
                ),
            )
            if source_type:
                self._add_source(
                    connection, int(task_id), source_type, source_ref,
                    source_event_id, excerpt,
                )
            updated = connection.execute("SELECT * FROM tasks WHERE task_id=?", (int(task_id),)).fetchone()
        return dict(updated)

    async def update(self, task_id: int, **kwargs) -> dict:
        return await asyncio.to_thread(self._update_sync, task_id, **kwargs)

    def _compact_sync(self) -> None:
        """Return deleted SQLite pages to disk after a permanent task purge."""
        try:
            connection = sqlite3.connect(self.db_path, timeout=30, isolation_level=None)
            try:
                connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                connection.execute("VACUUM")
            finally:
                connection.close()
        except sqlite3.Error as error:
            logger.warning("Task database compaction deferred: %s", error)

    @staticmethod
    def _hard_delete_rows(connection: sqlite3.Connection, task_ids: list[int]) -> None:
        if not task_ids:
            return
        placeholders = ",".join("?" for _ in task_ids)
        connection.execute(
            f"DELETE FROM task_sources WHERE task_id IN ({placeholders})", task_ids
        )
        connection.execute(
            f"DELETE FROM task_history WHERE task_id IN ({placeholders})", task_ids
        )
        connection.execute(f"DELETE FROM tasks WHERE task_id IN ({placeholders})", task_ids)

    def _delete_sync(self, task_id: int) -> dict:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM tasks WHERE task_id=? AND deleted_at IS NULL", (int(task_id),)
            ).fetchone()
            if row is None:
                raise ValueError(f"找不到未竟事项 #{task_id}。")
            self._hard_delete_rows(connection, [int(task_id)])
        item = dict(row)
        item["deleted_permanently"] = True
        self._compact_sync()
        return item

    async def delete(self, task_id: int) -> dict:
        return await asyncio.to_thread(self._delete_sync, task_id)

    def _purge_completed_sync(self, retention_days: int) -> dict:
        days = max(1, int(retention_days))
        cutoff = (beijing_now() - timedelta(days=days)).isoformat(timespec="seconds")
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            rows = connection.execute(
                """
                SELECT task_id FROM tasks
                WHERE deleted_at IS NULL AND status='completed'
                    AND completion_notice_pending=0
                    AND completed_at IS NOT NULL AND completed_at<=?
                ORDER BY completed_at, task_id
                """,
                (cutoff,),
            ).fetchall()
            task_ids = [int(row["task_id"]) for row in rows]
            self._hard_delete_rows(connection, task_ids)
        if task_ids:
            self._compact_sync()
        return {"deleted": len(task_ids), "task_ids": task_ids, "cutoff": cutoff}

    async def purge_completed(self, retention_days: int) -> dict:
        return await asyncio.to_thread(self._purge_completed_sync, retention_days)

    def _history_sync(self, task_id: int, limit: int = 50) -> list[dict]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM task_history WHERE task_id=? ORDER BY history_id DESC LIMIT ?",
                (int(task_id), max(1, min(200, int(limit)))),
            ).fetchall()
        return [dict(row) for row in rows]

    async def history(self, task_id: int, limit: int = 50) -> list[dict]:
        return await asyncio.to_thread(self._history_sync, task_id, limit)

    def _count_sync(self) -> dict:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT status, COUNT(*) AS count FROM tasks WHERE deleted_at IS NULL GROUP BY status"
            ).fetchall()
        counts = {"open": 0, "completed": 0, "cancelled": 0}
        counts.update({row["status"]: int(row["count"]) for row in rows})
        counts["total"] = sum(counts.values())
        return counts

    async def counts(self) -> dict:
        return await asyncio.to_thread(self._count_sync)

    def _pending_completions_sync(self, limit: int) -> list[dict]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM tasks WHERE deleted_at IS NULL AND status='completed'
                    AND completion_notice_pending=1
                ORDER BY completed_at DESC, task_id DESC LIMIT ?
                """,
                (max(1, min(20, int(limit))),),
            ).fetchall()
        return [dict(row) for row in rows]

    async def pending_completions(self, limit: int = 5) -> list[dict]:
        return await asyncio.to_thread(self._pending_completions_sync, limit)

    def _mark_completions_delivered_sync(self, task_ids: list[int]) -> None:
        ids = [int(item) for item in task_ids if int(item) > 0]
        if not ids:
            return
        placeholders = ",".join("?" for _ in ids)
        with self._connect() as connection:
            connection.execute(
                f"UPDATE tasks SET completion_notice_pending=0 WHERE task_id IN ({placeholders})",
                ids,
            )

    async def mark_completions_delivered(self, task_ids: list[int]) -> None:
        await asyncio.to_thread(self._mark_completions_delivered_sync, task_ids)

    def _event_sync(self, event_key: str) -> dict | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM task_events WHERE event_key=?", (str(event_key),)
            ).fetchone()
        return self._row(row)

    async def event(self, event_key: str) -> dict | None:
        return await asyncio.to_thread(self._event_sync, event_key)

    def _recent_hash_sync(self, content_hash: str, since: str) -> dict | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT * FROM task_events WHERE content_hash=? AND created_at>=?
                    AND status='applied' ORDER BY event_id DESC LIMIT 1
                """,
                (content_hash, since),
            ).fetchone()
        return self._row(row)

    async def recent_hash(self, content_hash: str, since: str) -> dict | None:
        return await asyncio.to_thread(self._recent_hash_sync, content_hash, since)

    def _save_event_sync(
        self,
        event_key: str,
        content_hash: str,
        source_type: str,
        source_ref: str,
        status: str,
        result_json: str = "",
        error: str = "",
    ) -> None:
        now = now_iso()
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO task_events (
                    event_key, content_hash, source_type, source_ref, status,
                    result_json, error, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(event_key) DO UPDATE SET status=excluded.status,
                    result_json=excluded.result_json, error=excluded.error,
                    updated_at=excluded.updated_at
                """,
                (
                    event_key, content_hash, source_type, source_ref, status,
                    result_json, error[:1000], now, now,
                ),
            )

    async def save_event(self, **kwargs) -> None:
        await asyncio.to_thread(self._save_event_sync, **kwargs)

    def _set_embedding_sync(
        self, task_id: int, vector: np.ndarray, model: str, digest: str
    ) -> None:
        array = np.asarray(vector, dtype=np.float32)
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE tasks SET embedding=?, embedding_model=?, embedding_dimensions=?,
                    embedding_hash=? WHERE task_id=?
                """,
                (array.astype("<f4", copy=False).tobytes(), model, len(array), digest, int(task_id)),
            )

    async def set_embedding(self, task_id: int, vector: np.ndarray, model: str, digest: str) -> None:
        await asyncio.to_thread(self._set_embedding_sync, task_id, vector, model, digest)

    def _vectors_sync(self) -> list[tuple[int, np.ndarray]]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT task_id, embedding FROM tasks WHERE deleted_at IS NULL AND embedding IS NOT NULL"
            ).fetchall()
        return [(int(row["task_id"]), np.frombuffer(row["embedding"], dtype="<f4")) for row in rows]

    async def vectors(self) -> list[tuple[int, np.ndarray]]:
        return await asyncio.to_thread(self._vectors_sync)
