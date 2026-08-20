"""Append-only, hash-chained audit events for memory sidecars."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import sqlite3
from pathlib import Path

from utils import now_iso


_SENSITIVE_KEY = re.compile(r"(?:key|token|secret|password|seal|credential)", re.I)


class ContinuityLedger:
    def __init__(self, config: dict):
        settings = config.get("continuity_ledger", {})
        self.enabled = bool(settings.get("enabled", True))
        self.db_path = settings.get("db_path") or os.path.join(
            config["buckets_dir"], "continuity.sqlite3"
        )
        if self.enabled:
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
                CREATE TABLE IF NOT EXISTS continuity_events (
                    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    created_at TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    source TEXT NOT NULL,
                    source_ref TEXT NOT NULL DEFAULT '',
                    payload_json TEXT NOT NULL DEFAULT '{}',
                    previous_hash TEXT NOT NULL DEFAULT '',
                    event_hash TEXT NOT NULL UNIQUE
                )
                """
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_continuity_created "
                "ON continuity_events(event_id DESC)"
            )

    @classmethod
    def _sanitize(cls, value):
        if isinstance(value, dict):
            return {
                str(key)[:80]: (
                    "[REDACTED]" if _SENSITIVE_KEY.search(str(key)) else cls._sanitize(item)
                )
                for key, item in value.items()
            }
        if isinstance(value, list):
            return [cls._sanitize(item) for item in value[:50]]
        if isinstance(value, (bool, int, float)) or value is None:
            return value
        return str(value)[:1000]

    def _append_sync(
        self, event_type: str, source: str, source_ref: str, payload: dict | None
    ) -> dict:
        created_at = now_iso()
        safe_payload = self._sanitize(payload or {})
        payload_json = json.dumps(
            safe_payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            previous = connection.execute(
                "SELECT event_hash FROM continuity_events ORDER BY event_id DESC LIMIT 1"
            ).fetchone()
            previous_hash = str(previous["event_hash"] if previous else "")
            canonical = "\0".join(
                (
                    previous_hash,
                    created_at,
                    str(event_type),
                    str(source),
                    str(source_ref or ""),
                    payload_json,
                )
            )
            event_hash = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
            cursor = connection.execute(
                """
                INSERT INTO continuity_events (
                    created_at, event_type, source, source_ref,
                    payload_json, previous_hash, event_hash
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    created_at,
                    str(event_type)[:80],
                    str(source)[:80],
                    str(source_ref or "")[:200],
                    payload_json,
                    previous_hash,
                    event_hash,
                ),
            )
        return {
            "event_id": int(cursor.lastrowid),
            "created_at": created_at,
            "event_hash": event_hash,
        }

    async def append(
        self, event_type: str, source: str, source_ref: str = "", payload: dict | None = None
    ) -> dict:
        if not self.enabled:
            return {"status": "disabled"}
        return await asyncio.to_thread(
            self._append_sync, event_type, source, source_ref, payload
        )

    def _list_sync(self, limit: int) -> list[dict]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM continuity_events ORDER BY event_id DESC LIMIT ?",
                (max(1, min(200, int(limit))),),
            ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["payload"] = json.loads(item.pop("payload_json", "{}"))
            result.append(item)
        return result

    async def list(self, limit: int = 30) -> list[dict]:
        if not self.enabled:
            return []
        return await asyncio.to_thread(self._list_sync, limit)

    def _verify_sync(self) -> dict:
        previous_hash = ""
        checked = 0
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM continuity_events ORDER BY event_id ASC"
            ).fetchall()
        for row in rows:
            canonical = "\0".join(
                (
                    previous_hash,
                    row["created_at"],
                    row["event_type"],
                    row["source"],
                    row["source_ref"],
                    row["payload_json"],
                )
            )
            expected = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
            if row["previous_hash"] != previous_hash or row["event_hash"] != expected:
                return {"valid": False, "checked": checked, "broken_at": row["event_id"]}
            previous_hash = row["event_hash"]
            checked += 1
        return {"valid": True, "checked": checked, "head": previous_hash}

    async def verify(self) -> dict:
        if not self.enabled:
            return {"valid": True, "checked": 0, "disabled": True}
        return await asyncio.to_thread(self._verify_sync)
