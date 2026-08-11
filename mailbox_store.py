from __future__ import annotations

import asyncio
import os
import sqlite3
from datetime import datetime, timedelta

from utils import (
    BEIJING_TIMEZONE,
    beijing_now,
    normalize_beijing_timestamp,
    now_iso,
)


class MailboxStore:
    """Window handoff messages stored separately from memory buckets."""

    def __init__(self, config: dict):
        settings = config.get("mailbox", {})
        self.db_path = settings.get("db_path") or os.path.join(
            config["buckets_dir"], "mailbox.sqlite3"
        )
        self.retention_days = max(0, int(settings.get("retention_days", 0)))
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
                CREATE TABLE IF NOT EXISTS mailbox_messages (
                    message_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    created_at TEXT NOT NULL,
                    message TEXT NOT NULL,
                    source_tool TEXT NOT NULL DEFAULT 'grow',
                    updated_at TEXT,
                    deleted_at TEXT
                )
                """
            )
            columns = {
                row[1]
                for row in connection.execute(
                    "PRAGMA table_info(mailbox_messages)"
                ).fetchall()
            }
            if "updated_at" not in columns:
                connection.execute(
                    "ALTER TABLE mailbox_messages ADD COLUMN updated_at TEXT"
                )
            if "deleted_at" not in columns:
                connection.execute(
                    "ALTER TABLE mailbox_messages ADD COLUMN deleted_at TEXT"
                )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS mailbox_message_history (
                    history_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    message_id INTEGER NOT NULL,
                    snapshot_at TEXT NOT NULL,
                    operation TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    message TEXT NOT NULL,
                    source_tool TEXT NOT NULL,
                    updated_at TEXT,
                    deleted_at TEXT,
                    CHECK (operation IN ('update', 'delete'))
                )
                """
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_mailbox_created "
                "ON mailbox_messages(message_id DESC)"
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_mailbox_active "
                "ON mailbox_messages(deleted_at, message_id DESC)"
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_mailbox_history_message "
                "ON mailbox_message_history(message_id, history_id DESC)"
            )

    def _add_sync(
        self, message: str, source_tool: str, created_at: str | None
    ) -> dict:
        text = message.strip()
        if not text:
            raise ValueError("mailbox message cannot be empty")
        timestamp = (
            normalize_beijing_timestamp(created_at)
            if created_at
            else now_iso()
        )
        with self._connect() as connection:
            cursor = connection.execute(
                "INSERT INTO mailbox_messages (created_at, message, source_tool) "
                "VALUES (?, ?, ?)",
                (timestamp, text, source_tool),
            )
            message_id = int(cursor.lastrowid)
        return {
            "message_id": message_id,
            "created_at": timestamp,
            "message": text,
            "source_tool": source_tool,
            "updated_at": None,
            "deleted_at": None,
        }

    async def add(
        self,
        message: str,
        source_tool: str = "grow",
        created_at: str | None = None,
    ) -> dict:
        saved = await asyncio.to_thread(
            self._add_sync, message, source_tool, created_at
        )
        await self.expire_old_messages()
        return saved

    @staticmethod
    def _parse_timestamp(value: str) -> datetime | None:
        try:
            parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        except (AttributeError, TypeError, ValueError):
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=BEIJING_TIMEZONE)
        return parsed.astimezone(BEIJING_TIMEZONE)

    def _expire_old_messages_sync(
        self, reference_time: datetime | None = None
    ) -> list[int]:
        if self.retention_days <= 0:
            return []

        current = beijing_now(reference_time)
        cutoff = current - timedelta(days=self.retention_days)
        deleted_at = current.isoformat(timespec="seconds")
        expired_ids = []

        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            rows = connection.execute(
                f"SELECT {self._select_columns()} FROM mailbox_messages "
                "WHERE deleted_at IS NULL ORDER BY message_id DESC"
            ).fetchall()
            if len(rows) <= 1:
                return []

            # The newest active handoff is always kept until another one arrives.
            for row in rows[1:]:
                created_at = self._parse_timestamp(row["created_at"])
                if created_at is None or created_at >= cutoff:
                    continue
                self._snapshot(connection, row, "delete", deleted_at)
                connection.execute(
                    "UPDATE mailbox_messages SET deleted_at = ? "
                    "WHERE message_id = ? AND deleted_at IS NULL",
                    (deleted_at, row["message_id"]),
                )
                expired_ids.append(int(row["message_id"]))
        return expired_ids

    async def expire_old_messages(
        self, reference_time: datetime | None = None
    ) -> list[int]:
        """Soft-delete expired messages while always preserving the newest one."""
        return await asyncio.to_thread(
            self._expire_old_messages_sync, reference_time
        )

    @staticmethod
    def _select_columns() -> str:
        return (
            "message_id, created_at, message, source_tool, "
            "updated_at, deleted_at"
        )

    def _get_sync(
        self, message_id: int, include_deleted: bool = False
    ) -> dict | None:
        safe_id = int(message_id)
        if safe_id <= 0:
            return None
        query = (
            f"SELECT {self._select_columns()} FROM mailbox_messages "
            "WHERE message_id = ?"
        )
        if not include_deleted:
            query += " AND deleted_at IS NULL"
        with self._connect() as connection:
            row = connection.execute(query, (safe_id,)).fetchone()
        return dict(row) if row else None

    async def get(
        self, message_id: int, include_deleted: bool = False
    ) -> dict | None:
        return await asyncio.to_thread(
            self._get_sync, message_id, include_deleted
        )

    @staticmethod
    def _snapshot(
        connection: sqlite3.Connection,
        row: sqlite3.Row,
        operation: str,
        snapshot_at: str,
    ) -> None:
        connection.execute(
            """
            INSERT INTO mailbox_message_history (
                message_id, snapshot_at, operation, created_at, message,
                source_tool, updated_at, deleted_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                row["message_id"],
                snapshot_at,
                operation,
                row["created_at"],
                row["message"],
                row["source_tool"],
                row["updated_at"],
                row["deleted_at"],
            ),
        )

    def _update_sync(self, message_id: int, message: str) -> dict:
        safe_id = int(message_id)
        text = message.strip()
        if safe_id <= 0:
            raise ValueError("mailbox message_id must be positive")
        if not text:
            raise ValueError("mailbox message cannot be empty")
        timestamp = now_iso()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                f"SELECT {self._select_columns()} FROM mailbox_messages "
                "WHERE message_id = ? AND deleted_at IS NULL",
                (safe_id,),
            ).fetchone()
            if not row:
                raise ValueError("mailbox message not found or already deleted")
            self._snapshot(connection, row, "update", timestamp)
            connection.execute(
                "UPDATE mailbox_messages SET message = ?, updated_at = ? "
                "WHERE message_id = ? AND deleted_at IS NULL",
                (text, timestamp, safe_id),
            )
            updated = connection.execute(
                f"SELECT {self._select_columns()} FROM mailbox_messages "
                "WHERE message_id = ?",
                (safe_id,),
            ).fetchone()
        return dict(updated)

    async def update(self, message_id: int, message: str) -> dict:
        return await asyncio.to_thread(self._update_sync, message_id, message)

    def _delete_sync(self, message_id: int) -> dict:
        safe_id = int(message_id)
        if safe_id <= 0:
            raise ValueError("mailbox message_id must be positive")
        timestamp = now_iso()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                f"SELECT {self._select_columns()} FROM mailbox_messages "
                "WHERE message_id = ? AND deleted_at IS NULL",
                (safe_id,),
            ).fetchone()
            if not row:
                raise ValueError("mailbox message not found or already deleted")
            self._snapshot(connection, row, "delete", timestamp)
            connection.execute(
                "UPDATE mailbox_messages SET deleted_at = ? "
                "WHERE message_id = ? AND deleted_at IS NULL",
                (timestamp, safe_id),
            )
            deleted = connection.execute(
                f"SELECT {self._select_columns()} FROM mailbox_messages "
                "WHERE message_id = ?",
                (safe_id,),
            ).fetchone()
        return dict(deleted)

    async def delete(self, message_id: int) -> dict:
        return await asyncio.to_thread(self._delete_sync, message_id)

    def _history_sync(self, message_id: int, limit: int) -> list[dict]:
        safe_id = int(message_id)
        safe_limit = max(1, min(100, int(limit)))
        if safe_id <= 0:
            return []
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT history_id, message_id, snapshot_at, operation,
                       created_at, message, source_tool, updated_at, deleted_at
                FROM mailbox_message_history
                WHERE message_id = ?
                ORDER BY history_id DESC
                LIMIT ?
                """,
                (safe_id, safe_limit),
            ).fetchall()
        return [dict(row) for row in rows]

    async def history(
        self, message_id: int, limit: int = 10
    ) -> list[dict]:
        return await asyncio.to_thread(self._history_sync, message_id, limit)

    def _list_sync(
        self, limit: int, before_id: int, include_deleted: bool = False
    ) -> list[dict]:
        safe_limit = max(1, min(100, int(limit)))
        safe_before = max(0, int(before_id))
        query = (
            f"SELECT {self._select_columns()} "
            "FROM mailbox_messages "
        )
        params = []
        conditions = []
        if not include_deleted:
            conditions.append("deleted_at IS NULL")
        if safe_before:
            conditions.append("message_id < ?")
            params.append(safe_before)
        if conditions:
            query += "WHERE " + " AND ".join(conditions) + " "
        query += "ORDER BY message_id DESC LIMIT ?"
        params.append(safe_limit)
        with self._connect() as connection:
            rows = connection.execute(query, params).fetchall()
        return [dict(row) for row in rows]

    async def list(
        self,
        limit: int = 10,
        before_id: int = 0,
        include_deleted: bool = False,
    ) -> list[dict]:
        await self.expire_old_messages()
        return await asyncio.to_thread(
            self._list_sync, limit, before_id, include_deleted
        )

    async def latest(self) -> dict | None:
        messages = await self.list(limit=1)
        return messages[0] if messages else None

    def _search_pool_sync(
        self, include_deleted: bool = False, limit: int = 1000
    ) -> list[dict]:
        safe_limit = max(1, min(5000, int(limit)))
        query = f"SELECT {self._select_columns()} FROM mailbox_messages "
        if not include_deleted:
            query += "WHERE deleted_at IS NULL "
        query += "ORDER BY message_id DESC LIMIT ?"
        with self._connect() as connection:
            rows = connection.execute(query, (safe_limit,)).fetchall()
        return [dict(row) for row in rows]

    async def search_pool(
        self, include_deleted: bool = False, limit: int = 1000
    ) -> list[dict]:
        """Read a bounded mailbox pool for retrieval without changing messages."""
        await self.expire_old_messages()
        return await asyncio.to_thread(
            self._search_pool_sync, include_deleted, limit
        )

    def count(self, include_deleted: bool = False) -> int:
        query = "SELECT COUNT(*) FROM mailbox_messages"
        if not include_deleted:
            query += " WHERE deleted_at IS NULL"
        with self._connect() as connection:
            row = connection.execute(query).fetchone()
        return int(row[0])
