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

    # ``status`` is kept as a compatibility column for existing databases.
    # The user-facing workflow lives in ``workflow_state`` so older rows can be
    # upgraded without rebuilding or risking the task ledger.
    WORKFLOW_STATES = {"planned", "in_progress", "waiting", "completed"}
    ACTIVE_WORKFLOW_STATES = {"planned", "in_progress", "waiting"}

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
            task_columns = {
                str(row["name"])
                for row in connection.execute("PRAGMA table_info(tasks)").fetchall()
            }
            if "workflow_state" not in task_columns:
                connection.execute(
                    "ALTER TABLE tasks ADD COLUMN workflow_state TEXT NOT NULL DEFAULT 'planned'"
                )
                connection.execute(
                    "UPDATE tasks SET workflow_state=CASE "
                    "WHEN status='completed' THEN 'completed' "
                    "WHEN status='cancelled' THEN 'completed' ELSE 'planned' END"
                )
            history_columns = {
                str(row["name"])
                for row in connection.execute("PRAGMA table_info(task_history)").fetchall()
            }
            if "workflow_state" not in history_columns:
                connection.execute(
                    "ALTER TABLE task_history ADD COLUMN workflow_state TEXT NOT NULL DEFAULT 'planned'"
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
    def _clean_workflow_state(cls, value: str) -> str:
        state = str(value or "planned").strip().lower()
        aliases = {
            "open": "planned",
            "cancelled": "completed",
            "未完成": "planned",
            "准备做": "planned",
            "正在做": "in_progress",
            "等待中": "waiting",
            "完成": "completed",
            "已完成": "completed",
        }
        state = aliases.get(state, state)
        if state not in cls.WORKFLOW_STATES:
            raise ValueError(
                "状态只能是 planned、in_progress、waiting 或 completed。"
            )
        return state

    @classmethod
    def _public_row(cls, row: sqlite3.Row | dict | None) -> dict | None:
        if row is None:
            return None
        item = dict(row)
        state = str(item.get("workflow_state") or "").strip().lower()
        if state not in cls.WORKFLOW_STATES:
            state = "completed" if item.get("status") in {"completed", "cancelled"} else "planned"
        item["workflow_state"] = state
        item["status"] = state
        return item

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
                created_by, manual_updated_at, workflow_state
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                row["task_id"], now_iso(), operation, row["title"], row["details"],
                row["status"], row["importance"], row["created_at"], row["updated_at"],
                row["completed_at"], row["created_by"], row["manual_updated_at"],
                row["workflow_state"] if "workflow_state" in row.keys() else (
                    "completed" if row["status"] in {"completed", "cancelled"} else "planned"
                ),
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
                    title, details, status, workflow_state, importance,
                    created_at, updated_at, created_by
                ) VALUES (?, ?, 'open', 'planned', ?, ?, ?, ?)
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
        return self._public_row(row)

    async def create(self, **kwargs) -> dict:
        return await asyncio.to_thread(self._create_sync, **kwargs)

    def _get_sync(self, task_id: int, include_deleted: bool = False) -> dict | None:
        where = "task_id=?" if include_deleted else "task_id=? AND deleted_at IS NULL"
        with self._connect() as connection:
            row = connection.execute(f"SELECT * FROM tasks WHERE {where}", (int(task_id),)).fetchone()
            if row is None:
                return None
            item = self._public_row(row) or {}
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
            requested = str(status).strip().lower()
            if requested == "open":
                clauses.append("status='open'")
            elif requested == "cancelled":
                clauses.append("status='cancelled'")
            else:
                workflow_state = self._clean_workflow_state(requested)
                clauses.append("workflow_state=?")
                params.append(workflow_state)
        if before_id:
            clauses.append("task_id<?")
            params.append(int(before_id))
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        params.append(max(1, min(500, int(limit))))
        with self._connect() as connection:
            rows = connection.execute(
                f"SELECT * FROM tasks {where} "
                "ORDER BY CASE workflow_state WHEN 'in_progress' THEN 0 WHEN 'waiting' THEN 1 "
                "WHEN 'planned' THEN 2 ELSE 3 END, "
                "importance DESC, updated_at DESC, task_id DESC LIMIT ?",
                params,
            ).fetchall()
        return [self._public_row(row) for row in rows]

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
            current_state = str(row["workflow_state"] or "planned")
            new_workflow_state = (
                self._clean_workflow_state(status) if status is not None else current_state
            )
            new_status = "completed" if new_workflow_state == "completed" else "open"
            new_importance = self._clean_importance(importance) if importance is not None else row["importance"]
            now = now_iso()
            completed_at = row["completed_at"]
            pending = int(row["completion_notice_pending"] or 0)
            if new_workflow_state == "completed" and current_state != "completed":
                completed_at = now
                pending = 1
            elif new_workflow_state != "completed":
                completed_at = None
                pending = 0
            manual_at = now if manual else row["manual_updated_at"]
            connection.execute(
                """
                UPDATE tasks SET title=?, details=?, status=?, workflow_state=?, importance=?, updated_at=?,
                    completed_at=?, manual_updated_at=?, completion_notice_pending=?
                WHERE task_id=?
                """,
                (
                    new_title, new_details, new_status, new_workflow_state, new_importance, now,
                    completed_at, manual_at, pending, int(task_id),
                ),
            )
            if source_type:
                self._add_source(
                    connection, int(task_id), source_type, source_ref,
                    source_event_id, excerpt,
                )
            updated = connection.execute("SELECT * FROM tasks WHERE task_id=?", (int(task_id),)).fetchone()
        return self._public_row(updated)

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
        item = self._public_row(row) or {}
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

    def _retract_source_sync(self, source_type: str, source_ref: str) -> dict:
        """Undo safe automatic task effects from a corrected source write.

        Manually edited tasks are never rolled back.  An automatically-created
        task is hidden only when the corrected write was its sole provenance.
        For an automatic status update, restore the latest snapshot only when
        no later manual edit exists and the source is still the latest change.
        """
        source_kind = str(source_type or "").strip()[:40]
        source_reference = str(source_ref or "").strip()[:160]
        result = {"sources_removed": 0, "tasks_retracted": [], "tasks_restored": [], "protected": []}
        if not source_kind:
            return result
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            sources = connection.execute(
                """
                SELECT * FROM task_sources
                WHERE source_type=? AND source_ref=?
                ORDER BY source_id DESC
                """,
                (source_kind, source_reference),
            ).fetchall()
            task_ids = sorted({int(row["task_id"]) for row in sources})
            latest_source_at = {
                int(task_id): max(
                    str(row["created_at"])
                    for row in sources if int(row["task_id"]) == int(task_id)
                )
                for task_id in task_ids
            }
            connection.execute(
                "DELETE FROM task_sources WHERE source_type=? AND source_ref=?",
                (source_kind, source_reference),
            )
            result["sources_removed"] = len(sources)
            stamp = now_iso()
            for task_id in task_ids:
                task = connection.execute(
                    "SELECT * FROM tasks WHERE task_id=? AND deleted_at IS NULL",
                    (task_id,),
                ).fetchone()
                if not task:
                    continue
                remaining = int(connection.execute(
                    "SELECT COUNT(*) FROM task_sources WHERE task_id=?", (task_id,)
                ).fetchone()[0])
                if task["manual_updated_at"]:
                    result["protected"].append(task_id)
                    continue
                if str(task["created_by"] or "") == "auto" and remaining == 0:
                    self._snapshot(connection, task, "source_correction_retract")
                    connection.execute(
                        "UPDATE tasks SET deleted_at=?, updated_at=? WHERE task_id=?",
                        (stamp, stamp, task_id),
                    )
                    result["tasks_retracted"].append(task_id)
                    continue
                history = connection.execute(
                    """
                    SELECT * FROM task_history
                    WHERE task_id=? AND operation='auto_update'
                    ORDER BY history_id DESC LIMIT 1
                    """,
                    (task_id,),
                ).fetchone()
                if (
                    history
                    and str(task["updated_at"] or "") <= latest_source_at[task_id]
                ):
                    self._snapshot(connection, task, "source_correction_restore")
                    connection.execute(
                        """
                            UPDATE tasks SET title=?, details=?, status=?, workflow_state=?, importance=?,
                            updated_at=?, completed_at=?, completion_notice_pending=0
                        WHERE task_id=?
                        """,
                        (
                            history["title"], history["details"], history["status"],
                            history["workflow_state"] if "workflow_state" in history.keys() else (
                                "completed" if history["status"] in {"completed", "cancelled"} else "planned"
                            ),
                            history["importance"], stamp, history["completed_at"], task_id,
                        ),
                    )
                    result["tasks_restored"].append(task_id)
            connection.execute(
                """
                UPDATE task_events SET status='superseded', updated_at=?
                WHERE source_type=? AND source_ref=? AND status IN ('applied','pending')
                """,
                (stamp, source_kind, source_reference),
            )
        return result

    async def retract_source(self, source_type: str, source_ref: str) -> dict:
        return await asyncio.to_thread(
            self._retract_source_sync, source_type, source_ref
        )

    def _count_sync(self) -> dict:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT workflow_state, COUNT(*) AS count FROM tasks "
                "WHERE deleted_at IS NULL GROUP BY workflow_state"
            ).fetchall()
        counts = {state: 0 for state in self.WORKFLOW_STATES}
        counts.update({row["workflow_state"]: int(row["count"]) for row in rows})
        counts["open"] = sum(counts[state] for state in self.ACTIVE_WORKFLOW_STATES)
        counts["cancelled"] = 0
        counts["total"] = sum(counts[state] for state in self.WORKFLOW_STATES)
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
        return [self._public_row(row) for row in rows]

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
