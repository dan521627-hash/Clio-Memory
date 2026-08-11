"""Persistent audit log for safe proxy behaviors and Bark delivery."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import secrets
import sqlite3
from contextlib import contextmanager
from datetime import timedelta

from utils import beijing_now, now_iso


class BehaviorStore:
    def __init__(self, config: dict):
        settings = config.get("behavior", {})
        self.db_path = settings.get("db_path") or os.environ.get(
            "OMBRE_BEHAVIOR_DB",
            os.path.join(config["buckets_dir"], "behavior.sqlite3"),
        )
        os.makedirs(os.path.dirname(os.path.abspath(self.db_path)), exist_ok=True)
        self._initialize()

    @contextmanager
    def _connect(self):
        connection = sqlite3.connect(self.db_path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA secure_delete=ON")
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _initialize(self):
        with self._connect() as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS behavior_actions (
                    action_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    cycle_id INTEGER NOT NULL,
                    stage_index INTEGER NOT NULL,
                    decided_at TEXT NOT NULL,
                    action_type TEXT NOT NULL DEFAULT 'message',
                    content TEXT NOT NULL,
                    status TEXT NOT NULL,
                    delivered_at TEXT,
                    provider TEXT NOT NULL DEFAULT 'bark',
                    error TEXT NOT NULL DEFAULT '',
                    context_json TEXT NOT NULL DEFAULT '{}',
                    UNIQUE(cycle_id, stage_index)
                )
                """
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_behavior_time "
                "ON behavior_actions(action_id DESC)"
            )
            action_columns = {
                row[1]
                for row in connection.execute(
                    "PRAGMA table_info(behavior_actions)"
                ).fetchall()
            }
            if "handoff_status" not in action_columns:
                connection.execute(
                    "ALTER TABLE behavior_actions "
                    "ADD COLUMN handoff_status TEXT NOT NULL DEFAULT 'legacy'"
                )
            if "acknowledged_at" not in action_columns:
                connection.execute(
                    "ALTER TABLE behavior_actions ADD COLUMN acknowledged_at TEXT"
                )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS behavior_candidates (
                    candidate_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    cycle_id INTEGER NOT NULL,
                    source_event_id INTEGER NOT NULL UNIQUE,
                    created_at TEXT NOT NULL,
                    due_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending',
                    attempts INTEGER NOT NULL DEFAULT 0,
                    event_context_json TEXT NOT NULL DEFAULT '[]',
                    decision_note TEXT NOT NULL DEFAULT '',
                    follow_up_required INTEGER NOT NULL DEFAULT 0,
                    hormone_name TEXT NOT NULL DEFAULT '',
                    hormone_drive REAL NOT NULL DEFAULT 0,
                    updated_at TEXT NOT NULL
                )
                """
            )
            candidate_columns = {
                row[1]
                for row in connection.execute(
                    "PRAGMA table_info(behavior_candidates)"
                ).fetchall()
            }
            candidate_migrations = {
                "follow_up_required": "INTEGER NOT NULL DEFAULT 0",
                "hormone_name": "TEXT NOT NULL DEFAULT ''",
                "hormone_drive": "REAL NOT NULL DEFAULT 0",
            }
            for column, declaration in candidate_migrations.items():
                if column not in candidate_columns:
                    connection.execute(
                        f"ALTER TABLE behavior_candidates ADD COLUMN {column} {declaration}"
                    )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_behavior_candidates_due "
                "ON behavior_candidates(status, due_at)"
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS behavior_fingerprints (
                    fingerprint_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    created_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    digest TEXT NOT NULL,
                    grams_json TEXT NOT NULL,
                    intent TEXT NOT NULL DEFAULT ''
                )
                """
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_behavior_fingerprint_expiry "
                "ON behavior_fingerprints(expires_at, fingerprint_id DESC)"
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS behavior_meta (
                    meta_key TEXT PRIMARY KEY,
                    meta_value TEXT NOT NULL
                )
                """
            )
            connection.execute(
                "INSERT OR IGNORE INTO behavior_meta(meta_key, meta_value) "
                "VALUES ('fingerprint_salt', ?)",
                (secrets.token_hex(32),),
            )

    def _get_for_stage_sync(self, cycle_id: int, stage_index: int) -> dict | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM behavior_actions WHERE cycle_id=? AND stage_index=?",
                (int(cycle_id), int(stage_index)),
            ).fetchone()
        return self._decode(row)

    async def get_for_stage(self, cycle_id: int, stage_index: int) -> dict | None:
        return await asyncio.to_thread(self._get_for_stage_sync, cycle_id, stage_index)

    @staticmethod
    def _decode(row) -> dict | None:
        if not row:
            return None
        item = dict(row)
        try:
            item["context"] = json.loads(item.pop("context_json", "{}"))
        except (TypeError, ValueError):
            item["context"] = {}
        return item

    def _record_sync(self, payload: dict) -> dict:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                INSERT OR IGNORE INTO behavior_actions (
                    cycle_id, stage_index, decided_at, action_type, content,
                    status, delivered_at, provider, error, context_json,
                    handoff_status
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    int(payload["cycle_id"]),
                    int(payload["stage_index"]),
                    payload.get("decided_at") or now_iso(),
                    str(payload.get("action_type", "message"))[:40],
                    str(payload.get("content", ""))[:500],
                    str(payload.get("status", "rehearsal"))[:40],
                    payload.get("delivered_at"),
                    "bark",
                    str(payload.get("error", ""))[:300],
                    json.dumps(payload.get("context", {}), ensure_ascii=False),
                    "pending" if payload.get("status") == "sent" else "none",
                ),
            )
            row = connection.execute(
                "SELECT * FROM behavior_actions WHERE cycle_id=? AND stage_index=?",
                (int(payload["cycle_id"]), int(payload["stage_index"])),
            ).fetchone()
        return self._decode(row)

    async def record(self, payload: dict) -> dict:
        return await asyncio.to_thread(self._record_sync, payload)

    def _list_sync(self, limit: int = 20, before_id: int = 0) -> list[dict]:
        safe_limit = max(1, min(100, int(limit)))
        query = "SELECT * FROM behavior_actions "
        params = []
        if int(before_id) > 0:
            query += "WHERE action_id < ? "
            params.append(int(before_id))
        query += "ORDER BY action_id DESC LIMIT ?"
        params.append(safe_limit)
        with self._connect() as connection:
            rows = connection.execute(query, params).fetchall()
        return [self._decode(row) for row in rows]

    async def list(self, limit: int = 20, before_id: int = 0) -> list[dict]:
        return await asyncio.to_thread(self._list_sync, limit, before_id)

    def _list_sent_for_cycle_sync(self, cycle_id: int, limit: int = 10) -> list[dict]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM behavior_actions
                WHERE cycle_id=? AND status='sent'
                ORDER BY action_id ASC LIMIT ?
                """,
                (int(cycle_id), max(1, min(30, int(limit)))),
            ).fetchall()
        return [self._decode(row) for row in rows]

    async def list_sent_for_cycle(self, cycle_id: int, limit: int = 10) -> list[dict]:
        """Return only outward actions actually delivered during one absence cycle."""
        return await asyncio.to_thread(
            self._list_sent_for_cycle_sync, cycle_id, limit
        )

    def _list_pending_handoff_sync(self, limit: int = 10) -> list[dict]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM behavior_actions
                WHERE status='sent' AND handoff_status='pending'
                ORDER BY action_id ASC LIMIT ?
                """,
                (max(1, min(30, int(limit))),),
            ).fetchall()
        return [self._decode(row) for row in rows]

    async def list_pending_handoff(self, limit: int = 10) -> list[dict]:
        """Return plaintext Bark messages that have not yet been handed off."""
        return await asyncio.to_thread(self._list_pending_handoff_sync, limit)

    @staticmethod
    def _interaction_phase(item: dict) -> str:
        context = item.get("context") or {}
        phase = str(context.get("phase") or "").strip().lower()
        if phase:
            return phase
        if (
            int(context.get("event_count", 0)) == 0
            and int(item.get("stage_index", 0)) in {1, 900_000}
        ):
            return "silence"
        return "absence"

    def _pending_handoff_summary_sync(self) -> dict:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT *
                FROM behavior_actions
                WHERE status='sent' AND handoff_status='pending'
                ORDER BY action_id DESC
                """
            ).fetchall()
        items = [self._decode(row) for row in rows]
        latest_item = items[0] if items else None
        latest = None
        if latest_item:
            latest = {
                "action_id": int(latest_item["action_id"]),
                "cycle_id": int(latest_item["cycle_id"]),
                "delivered_at": latest_item.get("delivered_at"),
                "acknowledged_at": latest_item.get("acknowledged_at"),
                "phase": self._interaction_phase(latest_item),
            }
        return {
            "available": bool(items),
            "count": len(items),
            "latest": latest,
            "acknowledged": bool(items)
            and all(item.get("acknowledged_at") for item in items),
        }

    async def pending_handoff_summary(self) -> dict:
        return await asyncio.to_thread(self._pending_handoff_summary_sync)

    def _acknowledge_pending_sync(self, action_id: int = 0) -> dict:
        stamp = now_iso()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            if int(action_id) > 0:
                rows = connection.execute(
                    """
                    SELECT * FROM behavior_actions
                    WHERE action_id=? AND status='sent'
                        AND handoff_status='pending' AND acknowledged_at IS NULL
                    """,
                    (int(action_id),),
                ).fetchall()
            else:
                rows = connection.execute(
                    """
                    SELECT * FROM behavior_actions
                    WHERE status='sent' AND handoff_status='pending'
                        AND acknowledged_at IS NULL
                    ORDER BY action_id DESC LIMIT 1
                    """
                ).fetchall()
            if not rows:
                return {
                    "status": "empty",
                    "count": 0,
                    "cycle_ids": [],
                    "action_ids": [],
                    "phase": "",
                }
            action_ids = [int(row["action_id"]) for row in rows]
            placeholders = ",".join("?" for _ in action_ids)
            connection.execute(
                f"""
                UPDATE behavior_actions SET acknowledged_at=?
                WHERE action_id IN ({placeholders})
                """,
                (stamp, *action_ids),
            )
        items = [self._decode(row) for row in rows]
        phases = [self._interaction_phase(item) for item in items]
        silence_action_ids = [
            int(item["action_id"])
            for item, phase in zip(items, phases)
            if phase == "silence"
        ]
        stateful_action_ids = [
            int(item["action_id"])
            for item, phase in zip(items, phases)
            if phase != "silence"
        ]
        return {
            "status": "acknowledged",
            "count": len(rows),
            "cycle_ids": sorted({int(row["cycle_id"]) for row in rows}),
            "stateful_cycle_ids": sorted(
                {
                    int(item["cycle_id"])
                    for item, phase in zip(items, phases)
                    if phase != "silence"
                }
            ),
            "action_ids": action_ids,
            "silence_action_ids": silence_action_ids,
            "stateful_action_ids": stateful_action_ids,
            "phase": phases[0] if len(set(phases)) == 1 else "mixed",
            "acknowledged_at": stamp,
        }

    async def acknowledge_pending(self, action_id: int = 0) -> dict:
        """Acknowledge one visible Bark action and report its interaction phase."""
        return await asyncio.to_thread(self._acknowledge_pending_sync, action_id)

    @staticmethod
    def _normalized(value: str) -> str:
        return "".join(str(value or "").casefold().split())

    @classmethod
    def _signature(cls, value: str, salt: str) -> tuple[str, list[str]]:
        normalized = cls._normalized(value)
        digest = hashlib.sha256((salt + normalized).encode("utf-8")).hexdigest()
        grams = {
            hashlib.sha256((salt + normalized[index:index + 2]).encode("utf-8")).hexdigest()[:20]
            for index in range(max(1, len(normalized) - 1))
            if normalized[index:index + 2]
        }
        return digest, sorted(grams)

    def _remember_fingerprint_sync(self, content: str, intent: str = "") -> None:
        moment = beijing_now()
        with self._connect() as connection:
            salt = connection.execute(
                "SELECT meta_value FROM behavior_meta WHERE meta_key='fingerprint_salt'"
            ).fetchone()[0]
            digest, grams = self._signature(content, salt)
            connection.execute(
                "DELETE FROM behavior_fingerprints WHERE expires_at<=?",
                (moment.isoformat(timespec="seconds"),),
            )
            connection.execute(
                """
                INSERT INTO behavior_fingerprints (
                    created_at, expires_at, digest, grams_json, intent
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (
                    moment.isoformat(timespec="seconds"),
                    (moment + timedelta(hours=48)).isoformat(timespec="seconds"),
                    digest,
                    json.dumps(grams, ensure_ascii=True),
                    str(intent or "")[:80],
                ),
            )

    async def remember_fingerprint(self, content: str, intent: str = "") -> None:
        await asyncio.to_thread(self._remember_fingerprint_sync, content, intent)

    def _similarity_sync(self, content: str, limit: int = 8) -> dict:
        moment = beijing_now().isoformat(timespec="seconds")
        with self._connect() as connection:
            connection.execute(
                "DELETE FROM behavior_fingerprints WHERE expires_at<=?", (moment,)
            )
            salt = connection.execute(
                "SELECT meta_value FROM behavior_meta WHERE meta_key='fingerprint_salt'"
            ).fetchone()[0]
            digest, grams = self._signature(content, salt)
            rows = connection.execute(
                """
                SELECT digest, grams_json, intent FROM behavior_fingerprints
                WHERE expires_at>? ORDER BY fingerprint_id DESC LIMIT ?
                """,
                (moment, max(1, min(30, int(limit)))),
            ).fetchall()
        current = set(grams)
        best = 0.0
        exact = False
        intents = []
        for row in rows:
            intents.append(str(row["intent"] or ""))
            if row["digest"] == digest:
                exact = True
                best = 1.0
                continue
            try:
                previous = set(json.loads(row["grams_json"] or "[]"))
            except (TypeError, ValueError):
                previous = set()
            union = current | previous
            if union:
                best = max(best, len(current & previous) / len(union))
        return {"similarity": round(best, 4), "exact": exact, "recent_intents": intents}

    async def similarity(self, content: str, limit: int = 8) -> dict:
        return await asyncio.to_thread(self._similarity_sync, content, limit)

    def _recent_intents_sync(self, limit: int = 8) -> list[str]:
        moment = beijing_now().isoformat(timespec="seconds")
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT intent FROM behavior_fingerprints
                WHERE expires_at>? AND intent<>''
                ORDER BY fingerprint_id DESC LIMIT ?
                """,
                (moment, max(1, min(30, int(limit)))),
            ).fetchall()
        return [str(row["intent"]) for row in rows]

    async def recent_intents(self, limit: int = 8) -> list[str]:
        return await asyncio.to_thread(self._recent_intents_sync, limit)

    def _purge_handoff_sync(self, action_ids: list[int]) -> int:
        safe_ids = sorted({int(value) for value in action_ids if int(value) > 0})
        if not safe_ids:
            return 0
        placeholders = ",".join("?" for _ in safe_ids)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            rows = connection.execute(
                f"SELECT stage_index FROM behavior_actions "
                f"WHERE handoff_status='pending' AND action_id IN ({placeholders})",
                safe_ids,
            ).fetchall()
            candidate_ids = sorted(
                {
                    int(row["stage_index"]) - 1_000_000
                    for row in rows
                    if int(row["stage_index"]) >= 1_000_000
                }
            )
            cursor = connection.execute(
                f"DELETE FROM behavior_actions "
                f"WHERE handoff_status='pending' AND action_id IN ({placeholders})",
                safe_ids,
            )
            if candidate_ids:
                candidate_placeholders = ",".join("?" for _ in candidate_ids)
                connection.execute(
                    f"DELETE FROM behavior_candidates "
                    f"WHERE candidate_id IN ({candidate_placeholders})",
                    candidate_ids,
                )
        with self._connect() as connection:
            connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        return int(cursor.rowcount)

    async def purge_handoff(self, action_ids: list[int]) -> int:
        """Hard-delete handed-off Bark plaintext; no snapshot is created."""
        return await asyncio.to_thread(self._purge_handoff_sync, action_ids)

    def _purge_cycle_candidates_sync(self, cycle_ids: list[int]) -> int:
        safe_ids = sorted({int(value) for value in cycle_ids if int(value) > 0})
        if not safe_ids:
            return 0
        placeholders = ",".join("?" for _ in safe_ids)
        with self._connect() as connection:
            cursor = connection.execute(
                f"DELETE FROM behavior_candidates WHERE cycle_id IN ({placeholders})",
                safe_ids,
            )
        return int(cursor.rowcount)

    async def purge_cycle_candidates(self, cycle_ids: list[int]) -> int:
        """Drop obsolete scheduling material after a push acknowledgement."""
        return await asyncio.to_thread(self._purge_cycle_candidates_sync, cycle_ids)

    @staticmethod
    def _decode_candidate(row) -> dict | None:
        if not row:
            return None
        item = dict(row)
        try:
            item["event_contexts"] = json.loads(
                item.pop("event_context_json", "[]")
            )
        except (TypeError, ValueError):
            item["event_contexts"] = []
        return item

    def _upsert_candidate_sync(self, payload: dict) -> dict:
        stamp = now_iso()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                INSERT INTO behavior_candidates (
                    cycle_id, source_event_id, created_at, due_at, expires_at,
                    status, attempts, event_context_json, decision_note,
                    follow_up_required, hormone_name, hormone_drive, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, 0, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(source_event_id) DO UPDATE SET
                    due_at=excluded.due_at,
                    expires_at=excluded.expires_at,
                    status=excluded.status,
                    event_context_json=excluded.event_context_json,
                    decision_note=excluded.decision_note,
                    follow_up_required=excluded.follow_up_required,
                    hormone_name=excluded.hormone_name,
                    hormone_drive=excluded.hormone_drive,
                    updated_at=excluded.updated_at
                """,
                (
                    int(payload["cycle_id"]),
                    int(payload["source_event_id"]),
                    payload.get("created_at") or stamp,
                    payload["due_at"],
                    payload["expires_at"],
                    str(payload.get("status", "pending"))[:40],
                    json.dumps(payload.get("event_contexts", []), ensure_ascii=False),
                    str(payload.get("decision_note", ""))[:500],
                    1 if payload.get("follow_up_required") else 0,
                    str(payload.get("hormone_name", ""))[:80],
                    max(0.0, min(1.0, float(payload.get("hormone_drive", 0.0)))),
                    stamp,
                ),
            )
            row = connection.execute(
                "SELECT * FROM behavior_candidates WHERE source_event_id=?",
                (int(payload["source_event_id"]),),
            ).fetchone()
        return self._decode_candidate(row)

    async def upsert_candidate(self, payload: dict) -> dict:
        return await asyncio.to_thread(self._upsert_candidate_sync, payload)

    def _due_candidates_sync(self, as_of: str, limit: int = 3) -> list[dict]:
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE behavior_candidates SET status='expired',
                    decision_note='候场时间已过，未发送', updated_at=?
                WHERE status IN ('pending', 'waiting') AND expires_at<=?
                """,
                (as_of, as_of),
            )
            rows = connection.execute(
                """
                SELECT * FROM behavior_candidates
                WHERE status IN ('pending', 'waiting')
                  AND due_at<=? AND expires_at>?
                ORDER BY due_at, candidate_id LIMIT ?
                """,
                (as_of, as_of, max(1, min(20, int(limit)))),
            ).fetchall()
        return [self._decode_candidate(row) for row in rows]

    async def due_candidates(self, as_of: str, limit: int = 3) -> list[dict]:
        return await asyncio.to_thread(self._due_candidates_sync, as_of, limit)

    def _update_candidate_sync(
        self, candidate_id: int, status: str, note: str = "", due_at: str = ""
    ) -> dict | None:
        fields = "status=?, decision_note=?, attempts=attempts+1, updated_at=?"
        params = [str(status)[:40], str(note)[:500], now_iso()]
        if due_at:
            fields += ", due_at=?"
            params.append(due_at)
        params.append(int(candidate_id))
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                f"UPDATE behavior_candidates SET {fields} WHERE candidate_id=?",
                params,
            )
            row = connection.execute(
                "SELECT * FROM behavior_candidates WHERE candidate_id=?",
                (int(candidate_id),),
            ).fetchone()
        return self._decode_candidate(row)

    async def update_candidate(
        self, candidate_id: int, status: str, note: str = "", due_at: str = ""
    ) -> dict | None:
        return await asyncio.to_thread(
            self._update_candidate_sync, candidate_id, status, note, due_at
        )

    def _cancel_cycle_sync(self, cycle_id: int, except_id: int = 0) -> int:
        with self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE behavior_candidates SET status='cancelled',
                    decision_note='本轮已经发送过一次，取消其余候场', updated_at=?
                WHERE cycle_id=? AND candidate_id<>?
                  AND status IN ('pending', 'waiting')
                """,
                (now_iso(), int(cycle_id), int(except_id)),
            )
        return int(cursor.rowcount)

    async def cancel_cycle(self, cycle_id: int, except_id: int = 0) -> int:
        return await asyncio.to_thread(self._cancel_cycle_sync, cycle_id, except_id)

    def _list_candidates_sync(self, limit: int = 30) -> list[dict]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM behavior_candidates ORDER BY candidate_id DESC LIMIT ?",
                (max(1, min(100, int(limit))),),
            ).fetchall()
        return [self._decode_candidate(row) for row in rows]

    async def list_candidates(self, limit: int = 30) -> list[dict]:
        return await asyncio.to_thread(self._list_candidates_sync, limit)

    def count(self) -> int:
        with self._connect() as connection:
            return int(connection.execute("SELECT COUNT(*) FROM behavior_actions").fetchone()[0])
