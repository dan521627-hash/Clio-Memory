"""Independent SQLite ledger for the AI treasury."""

from __future__ import annotations

import asyncio
import os
import sqlite3
from decimal import Decimal, InvalidOperation
from pathlib import Path

from utils import normalize_beijing_timestamp, now_iso


class TreasuryStore:
    """Store income and expenses without mixing them into memory buckets."""

    ENTRY_TYPES = {"income", "expense"}

    def __init__(self, config: dict):
        settings = config.get("treasury", {})
        self.db_path = settings.get("db_path") or os.environ.get(
            "OMBRE_TREASURY_DB",
            os.path.join(config["buckets_dir"], "treasury.sqlite3"),
        )
        self.currency = str(settings.get("currency", "CNY")).strip() or "CNY"
        self.symbol = str(settings.get("symbol", "¥")).strip() or "¥"
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.db_path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA synchronous=NORMAL")
        return connection

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS treasury_entries (
                    entry_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    entry_type TEXT NOT NULL,
                    amount_cents INTEGER NOT NULL,
                    reason TEXT NOT NULL,
                    occurred_at TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    source TEXT NOT NULL,
                    updated_at TEXT,
                    deleted_at TEXT,
                    CHECK (entry_type IN ('income', 'expense')),
                    CHECK (amount_cents > 0)
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS treasury_entry_history (
                    history_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    entry_id INTEGER NOT NULL,
                    snapshot_at TEXT NOT NULL,
                    operation TEXT NOT NULL,
                    entry_type TEXT NOT NULL,
                    amount_cents INTEGER NOT NULL,
                    reason TEXT NOT NULL,
                    occurred_at TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    source TEXT NOT NULL,
                    updated_at TEXT,
                    deleted_at TEXT,
                    CHECK (operation IN ('update', 'delete'))
                )
                """
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_treasury_active_time "
                "ON treasury_entries(deleted_at, occurred_at DESC, entry_id DESC)"
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_treasury_type "
                "ON treasury_entries(entry_type, deleted_at, entry_id DESC)"
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_treasury_history "
                "ON treasury_entry_history(entry_id, history_id DESC)"
            )

    @staticmethod
    def _normalize_type(value: str) -> str:
        entry_type = str(value or "").strip().lower()
        aliases = {
            "收入": "income",
            "收": "income",
            "支出": "expense",
            "花费": "expense",
            "花": "expense",
        }
        entry_type = aliases.get(entry_type, entry_type)
        if entry_type not in TreasuryStore.ENTRY_TYPES:
            raise ValueError("类型只能是 income（收入）或 expense（支出）。")
        return entry_type

    @staticmethod
    def _normalize_amount(value) -> int:
        try:
            amount = Decimal(str(value).strip())
        except (InvalidOperation, ValueError):
            raise ValueError("金额必须是有效数字。") from None
        if not amount.is_finite() or amount <= 0:
            raise ValueError("金额必须大于 0。")
        cents = amount * 100
        if cents != cents.to_integral_value():
            raise ValueError("金额最多保留两位小数。")
        amount_cents = int(cents)
        if amount_cents > 99_999_999_999:
            raise ValueError("单笔金额过大。")
        return amount_cents

    @staticmethod
    def _normalize_reason(value: str) -> str:
        reason = " ".join(str(value or "").strip().split())
        if not reason:
            raise ValueError("必须写清楚这笔收入或支出的原因。")
        if len(reason) > 500:
            raise ValueError("原因不能超过 500 个字符。")
        return reason

    @staticmethod
    def _normalize_time(value: str | None) -> str:
        text = str(value or "").strip()
        if not text:
            return now_iso()
        try:
            return normalize_beijing_timestamp(text)
        except ValueError:
            raise ValueError("日期时间必须是有效的 ISO 格式。") from None

    @staticmethod
    def _select_columns() -> str:
        return (
            "entry_id, entry_type, amount_cents, reason, occurred_at, "
            "created_at, source, updated_at, deleted_at"
        )

    @staticmethod
    def _format_entry(row: sqlite3.Row | dict) -> dict:
        item = dict(row)
        item["amount"] = f"{int(item['amount_cents']) / 100:.2f}"
        return item

    def _summary_with_connection(self, connection: sqlite3.Connection) -> dict:
        row = connection.execute(
            """
            SELECT
                COALESCE(SUM(CASE WHEN entry_type='income' THEN amount_cents ELSE 0 END), 0)
                    AS income_cents,
                COALESCE(SUM(CASE WHEN entry_type='expense' THEN amount_cents ELSE 0 END), 0)
                    AS expense_cents,
                COUNT(*) AS entry_count
            FROM treasury_entries
            WHERE deleted_at IS NULL
            """
        ).fetchone()
        income_cents = int(row["income_cents"])
        expense_cents = int(row["expense_cents"])
        balance_cents = income_cents - expense_cents
        return {
            "currency": self.currency,
            "symbol": self.symbol,
            "balance_cents": balance_cents,
            "income_cents": income_cents,
            "expense_cents": expense_cents,
            "balance": f"{balance_cents / 100:.2f}",
            "total_income": f"{income_cents / 100:.2f}",
            "total_expense": f"{expense_cents / 100:.2f}",
            "entry_count": int(row["entry_count"]),
        }

    def _summary_sync(self) -> dict:
        with self._connect() as connection:
            return self._summary_with_connection(connection)

    async def summary(self) -> dict:
        return await asyncio.to_thread(self._summary_sync)

    def _record_sync(
        self,
        entry_type: str,
        amount,
        reason: str,
        occurred_at: str | None,
        source: str,
    ) -> dict:
        normalized_type = self._normalize_type(entry_type)
        amount_cents = self._normalize_amount(amount)
        normalized_reason = self._normalize_reason(reason)
        normalized_time = self._normalize_time(occurred_at)
        created_at = now_iso()
        normalized_source = str(source or "mcp").strip()[:80] or "mcp"
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            cursor = connection.execute(
                """
                INSERT INTO treasury_entries (
                    entry_type, amount_cents, reason, occurred_at,
                    created_at, source
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    normalized_type,
                    amount_cents,
                    normalized_reason,
                    normalized_time,
                    created_at,
                    normalized_source,
                ),
            )
            row = connection.execute(
                f"SELECT {self._select_columns()} FROM treasury_entries "
                "WHERE entry_id=?",
                (int(cursor.lastrowid),),
            ).fetchone()
            summary = self._summary_with_connection(connection)
        return {"entry": self._format_entry(row), "summary": summary}

    async def record(
        self,
        entry_type: str,
        amount,
        reason: str,
        occurred_at: str | None = None,
        source: str = "mcp",
    ) -> dict:
        return await asyncio.to_thread(
            self._record_sync,
            entry_type,
            amount,
            reason,
            occurred_at,
            source,
        )

    def _get_sync(
        self, entry_id: int, include_deleted: bool = False
    ) -> dict | None:
        safe_id = int(entry_id)
        if safe_id <= 0:
            return None
        query = (
            f"SELECT {self._select_columns()} FROM treasury_entries "
            "WHERE entry_id=?"
        )
        if not include_deleted:
            query += " AND deleted_at IS NULL"
        with self._connect() as connection:
            row = connection.execute(query, (safe_id,)).fetchone()
        return self._format_entry(row) if row else None

    async def get(
        self, entry_id: int, include_deleted: bool = False
    ) -> dict | None:
        return await asyncio.to_thread(self._get_sync, entry_id, include_deleted)

    def _list_sync(
        self,
        limit: int,
        before_id: int,
        entry_type: str,
        include_deleted: bool,
    ) -> list[dict]:
        safe_limit = max(1, min(100, int(limit)))
        safe_before = max(0, int(before_id))
        normalized_type = (
            self._normalize_type(entry_type) if str(entry_type).strip() else ""
        )
        conditions = []
        params: list = []
        if not include_deleted:
            conditions.append("deleted_at IS NULL")
        if normalized_type:
            conditions.append("entry_type=?")
            params.append(normalized_type)
        if safe_before:
            conditions.append("entry_id<?")
            params.append(safe_before)
        query = f"SELECT {self._select_columns()} FROM treasury_entries "
        if conditions:
            query += "WHERE " + " AND ".join(conditions) + " "
        query += "ORDER BY entry_id DESC LIMIT ?"
        params.append(safe_limit)
        with self._connect() as connection:
            rows = connection.execute(query, params).fetchall()
        return [self._format_entry(row) for row in rows]

    async def list(
        self,
        limit: int = 20,
        before_id: int = 0,
        entry_type: str = "",
        include_deleted: bool = False,
    ) -> list[dict]:
        return await asyncio.to_thread(
            self._list_sync,
            limit,
            before_id,
            entry_type,
            include_deleted,
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
            INSERT INTO treasury_entry_history (
                entry_id, snapshot_at, operation, entry_type,
                amount_cents, reason, occurred_at, created_at,
                source, updated_at, deleted_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                row["entry_id"],
                snapshot_at,
                operation,
                row["entry_type"],
                row["amount_cents"],
                row["reason"],
                row["occurred_at"],
                row["created_at"],
                row["source"],
                row["updated_at"],
                row["deleted_at"],
            ),
        )

    def _update_sync(
        self,
        entry_id: int,
        entry_type: str | None,
        amount,
        reason: str | None,
        occurred_at: str | None,
    ) -> dict:
        safe_id = int(entry_id)
        if safe_id <= 0:
            raise ValueError("entry_id 必须是正整数。")
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                f"SELECT {self._select_columns()} FROM treasury_entries "
                "WHERE entry_id=? AND deleted_at IS NULL",
                (safe_id,),
            ).fetchone()
            if not row:
                raise ValueError("找不到这笔账，或它已经删除。")
            next_type = (
                self._normalize_type(entry_type)
                if entry_type is not None and str(entry_type).strip()
                else row["entry_type"]
            )
            next_amount = (
                self._normalize_amount(amount)
                if amount is not None and str(amount).strip()
                else int(row["amount_cents"])
            )
            next_reason = (
                self._normalize_reason(reason)
                if reason is not None
                else row["reason"]
            )
            next_time = (
                self._normalize_time(occurred_at)
                if occurred_at is not None and str(occurred_at).strip()
                else row["occurred_at"]
            )
            if (
                next_type == row["entry_type"]
                and next_amount == row["amount_cents"]
                and next_reason == row["reason"]
                and next_time == row["occurred_at"]
            ):
                return {
                    "entry": self._format_entry(row),
                    "summary": self._summary_with_connection(connection),
                    "unchanged": True,
                }
            timestamp = now_iso()
            self._snapshot(connection, row, "update", timestamp)
            connection.execute(
                """
                UPDATE treasury_entries
                SET entry_type=?, amount_cents=?, reason=?,
                    occurred_at=?, updated_at=?
                WHERE entry_id=? AND deleted_at IS NULL
                """,
                (
                    next_type,
                    next_amount,
                    next_reason,
                    next_time,
                    timestamp,
                    safe_id,
                ),
            )
            updated = connection.execute(
                f"SELECT {self._select_columns()} FROM treasury_entries "
                "WHERE entry_id=?",
                (safe_id,),
            ).fetchone()
            summary = self._summary_with_connection(connection)
        return {"entry": self._format_entry(updated), "summary": summary}

    async def update(
        self,
        entry_id: int,
        entry_type: str | None = None,
        amount=None,
        reason: str | None = None,
        occurred_at: str | None = None,
    ) -> dict:
        return await asyncio.to_thread(
            self._update_sync,
            entry_id,
            entry_type,
            amount,
            reason,
            occurred_at,
        )

    def _delete_sync(self, entry_id: int) -> dict:
        safe_id = int(entry_id)
        if safe_id <= 0:
            raise ValueError("entry_id 必须是正整数。")
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                f"SELECT {self._select_columns()} FROM treasury_entries "
                "WHERE entry_id=? AND deleted_at IS NULL",
                (safe_id,),
            ).fetchone()
            if not row:
                raise ValueError("找不到这笔账，或它已经删除。")
            timestamp = now_iso()
            self._snapshot(connection, row, "delete", timestamp)
            connection.execute(
                "UPDATE treasury_entries SET deleted_at=? "
                "WHERE entry_id=? AND deleted_at IS NULL",
                (timestamp, safe_id),
            )
            deleted = connection.execute(
                f"SELECT {self._select_columns()} FROM treasury_entries "
                "WHERE entry_id=?",
                (safe_id,),
            ).fetchone()
            summary = self._summary_with_connection(connection)
        return {"entry": self._format_entry(deleted), "summary": summary}

    async def delete(self, entry_id: int) -> dict:
        return await asyncio.to_thread(self._delete_sync, entry_id)

    def _history_sync(self, entry_id: int, limit: int) -> list[dict]:
        safe_id = int(entry_id)
        safe_limit = max(1, min(100, int(limit)))
        if safe_id <= 0:
            return []
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT history_id, entry_id, snapshot_at, operation,
                       entry_type, amount_cents, reason, occurred_at,
                       created_at, source, updated_at, deleted_at
                FROM treasury_entry_history
                WHERE entry_id=?
                ORDER BY history_id DESC
                LIMIT ?
                """,
                (safe_id, safe_limit),
            ).fetchall()
        return [self._format_entry(row) for row in rows]

    async def history(self, entry_id: int, limit: int = 20) -> list[dict]:
        return await asyncio.to_thread(self._history_sync, entry_id, limit)
