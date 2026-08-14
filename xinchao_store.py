"""Persistent Xinchao state, event idempotency, flashes, and boot handoff."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
import sqlite3
import threading
from datetime import datetime, timedelta

from utils import beijing_now, now_iso
from xinchao_engine import PIPE_NAMES, XinchaoEngine, empty_pipes, parse_timestamp
from xinchao_evaluator import XinchaoEvaluator


logger = logging.getLogger("ombre_brain.xinchao")


class _ClosingConnection(sqlite3.Connection):
    """Commit or roll back like sqlite3's context manager, then close it."""

    def __exit__(self, exc_type, exc_value, traceback):
        try:
            return super().__exit__(exc_type, exc_value, traceback)
        finally:
            self.close()


class XinchaoService:
    _schema_lock = threading.Lock()

    def __init__(self, config: dict):
        self.config = config
        settings = config.get("xinchao", {})
        self.enabled = bool(settings.get("enabled", True))
        self.db_path = settings.get("db_path") or os.path.join(
            config["buckets_dir"], "xinchao.sqlite3"
        )
        self.exact_dedupe_hours = max(
            1.0, float(settings.get("exact_dedupe_hours", 24.0))
        )
        self.paraphrase_dedupe_seconds = max(
            30, int(settings.get("paraphrase_dedupe_seconds", 600))
        )
        self.flash_hours = max(1.0, float(settings.get("flash_hours", 24.0)))
        self.obsession_hours = max(
            self.flash_hours, float(settings.get("obsession_hours", 168.0))
        )
        self.obsession_repeats = max(2, int(settings.get("obsession_repeats", 3)))
        self.monologue_enabled = bool(settings.get("monologue_enabled", True))
        self.monologue_after_hours = max(
            0.0, float(settings.get("monologue_after_hours", 2.0))
        )
        raw_stages = settings.get("darkflow_stage_hours")
        if raw_stages is None:
            raw_stages = [
                self.monologue_after_hours,
                2,
                4,
                6,
                8,
                10,
                12,
            ]
        stages = []
        for value in raw_stages if isinstance(raw_stages, list) else []:
            try:
                hour = max(0.0, min(48.0, float(value)))
            except (TypeError, ValueError):
                continue
            if not stages or hour > stages[-1]:
                stages.append(hour)
        self.darkflow_stage_hours = stages or [1, 2, 4, 6, 8, 10, 12]
        self.presence_nudge_after_hours = max(
            0.1,
            min(
                4.0,
                float(settings.get("presence_nudge_after_minutes", 30)) / 60.0,
            ),
        )
        self.silence_to_absence_hours = max(
            self.presence_nudge_after_hours,
            min(
                12.0,
                float(settings.get("silence_to_absence_minutes", 60)) / 60.0,
            ),
        )
        self.darkflow_max_chars = max(
            200, min(400, int(settings.get("darkflow_max_chars", 400)))
        )
        self.drowsy_after_hours = max(
            0.0, float(settings.get("drowsy_after_hours", 4.0))
        )
        self.sleep_after_hours = max(
            self.drowsy_after_hours,
            float(settings.get("sleep_after_hours", 7.0)),
        )
        self.deep_sleep_after_hours = self.darkflow_stage_hours[-1]
        self.boot_once_hours = max(
            1.0, min(168.0, float(settings.get("boot_once_hours", 12.0)))
        )
        self.arrival_gap_minutes = max(
            30, min(360, int(settings.get("arrival_gap_minutes", 90)))
        )
        self.rhythm_min_samples = max(
            3, min(100, int(settings.get("rhythm_min_samples", 8)))
        )
        self.longing_after_hours = max(
            1.0, float(settings.get("longing_after_hours", 6.0))
        )
        self.longing_full_hours = max(
            self.longing_after_hours + 1.0,
            float(settings.get("longing_full_hours", 18.0)),
        )
        self.engine = XinchaoEngine(config)
        self.evaluator = XinchaoEvaluator(config)
        self.memory_resonance_provider = None
        self.task_context_provider = None
        self._process_lock = asyncio.Lock()
        if self.enabled:
            os.makedirs(os.path.dirname(os.path.abspath(self.db_path)), exist_ok=True)
            self._initialize()

    def set_memory_resonance_provider(self, provider) -> None:
        self.memory_resonance_provider = provider

    def set_task_context_provider(self, provider) -> None:
        self.task_context_provider = provider

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(
            self.db_path,
            timeout=30,
            factory=_ClosingConnection,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout=30000")
        return connection

    def _initialize(self) -> None:
        with self._schema_lock, self._connect() as connection:
            connection.execute("PRAGMA journal_mode=WAL")

            def add_column(table: str, column: str, declaration: str) -> None:
                try:
                    connection.execute(
                        f"ALTER TABLE {table} ADD COLUMN {column} {declaration}"
                    )
                except sqlite3.OperationalError as error:
                    if "duplicate column name" not in str(error).casefold():
                        raise

            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS xinchao_state (
                    state_id INTEGER PRIMARY KEY CHECK (state_id = 1),
                    cycle_id INTEGER NOT NULL DEFAULT 0,
                    cycle_open INTEGER NOT NULL DEFAULT 0,
                    last_event_at TEXT,
                    pipes_json TEXT NOT NULL,
                    last_event_summary TEXT NOT NULL DEFAULT '',
                    last_event_tag TEXT NOT NULL DEFAULT '',
                    updated_at TEXT NOT NULL,
                    version INTEGER NOT NULL DEFAULT 0
                )
                """
            )
            state_columns = {
                row[1]
                for row in connection.execute("PRAGMA table_info(xinchao_state)").fetchall()
            }
            state_migrations = {
                "last_presence_at": "TEXT",
                "cycle_origin": "TEXT NOT NULL DEFAULT 'event'",
                "sleep_stage": "TEXT NOT NULL DEFAULT 'awake'",
                "sleep_started_at": "TEXT",
                "deep_sleep_at": "TEXT",
                "darkflow_stage": "INTEGER NOT NULL DEFAULT 0",
                "last_darkflow_at": "TEXT",
                "darkflow_retry_at": "TEXT",
                "darkflow_failures": "INTEGER NOT NULL DEFAULT 0",
            }
            for column, declaration in state_migrations.items():
                if column not in state_columns:
                    add_column("xinchao_state", column, declaration)
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS xinchao_events (
                    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    created_at TEXT NOT NULL,
                    source_tool TEXT NOT NULL,
                    source_ref TEXT NOT NULL DEFAULT '',
                    fingerprint TEXT NOT NULL,
                    content TEXT,
                    event_summary TEXT NOT NULL DEFAULT '',
                    event_tag TEXT NOT NULL DEFAULT '',
                    context_card TEXT NOT NULL DEFAULT '',
                    cycle_id INTEGER NOT NULL DEFAULT 0,
                    canonical_tag TEXT NOT NULL DEFAULT '',
                    severity REAL NOT NULL DEFAULT 0,
                    deltas_json TEXT NOT NULL DEFAULT '{}',
                    narrative_complete INTEGER,
                    quality_note TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL DEFAULT 'pending',
                    error TEXT NOT NULL DEFAULT '',
                    attempt_count INTEGER NOT NULL DEFAULT 0,
                    next_retry_at TEXT,
                    prompt_hash TEXT NOT NULL DEFAULT '',
                    processed_at TEXT
                )
                """
            )
            event_columns = {
                row[1]
                for row in connection.execute("PRAGMA table_info(xinchao_events)").fetchall()
            }
            if "attempt_count" not in event_columns:
                add_column(
                    "xinchao_events", "attempt_count", "INTEGER NOT NULL DEFAULT 0"
                )
            if "next_retry_at" not in event_columns:
                add_column("xinchao_events", "next_retry_at", "TEXT")
            if "context_card" not in event_columns:
                add_column(
                    "xinchao_events", "context_card", "TEXT NOT NULL DEFAULT ''"
                )
            if "cycle_id" not in event_columns:
                add_column(
                    "xinchao_events", "cycle_id", "INTEGER NOT NULL DEFAULT 0"
                )
            if "external_event_hash" not in event_columns:
                add_column("xinchao_events", "external_event_hash", "TEXT")
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS xinchao_thoughts (
                    canonical_tag TEXT PRIMARY KEY,
                    event_tag TEXT NOT NULL,
                    first_seen TEXT NOT NULL,
                    last_seen TEXT NOT NULL,
                    occurrence_count INTEGER NOT NULL,
                    status TEXT NOT NULL,
                    floor_json TEXT NOT NULL DEFAULT '{}',
                    expires_at TEXT NOT NULL
                )
                """
            )
            thought_columns = {
                row[1]
                for row in connection.execute("PRAGMA table_info(xinchao_thoughts)").fetchall()
            }
            thought_migrations = {
                "thought_text": "TEXT NOT NULL DEFAULT ''",
                "tone": "TEXT NOT NULL DEFAULT 'mixed'",
                "intensity": "REAL NOT NULL DEFAULT 0.3",
                "reason": "TEXT NOT NULL DEFAULT ''",
                "source_event_id": "INTEGER",
                "source_tool": "TEXT NOT NULL DEFAULT ''",
                "source_ref": "TEXT NOT NULL DEFAULT ''",
                "privacy": "TEXT NOT NULL DEFAULT 'inner_only'",
                "resolved_at": "TEXT",
                "updated_at": "TEXT",
            }
            for column, declaration in thought_migrations.items():
                if column not in thought_columns:
                    add_column("xinchao_thoughts", column, declaration)
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS xinchao_arrival_rhythm (
                    hour INTEGER PRIMARY KEY CHECK (hour BETWEEN 0 AND 23),
                    weight REAL NOT NULL DEFAULT 0,
                    sample_count INTEGER NOT NULL DEFAULT 0,
                    updated_at TEXT NOT NULL
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS xinchao_boot_deliveries (
                    session_hash TEXT PRIMARY KEY,
                    delivered_at TEXT NOT NULL,
                    body_digest TEXT NOT NULL DEFAULT '',
                    body_chars INTEGER NOT NULL DEFAULT 0
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS xinchao_transitions (
                    transition_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    created_at TEXT NOT NULL,
                    transition_type TEXT NOT NULL,
                    cycle_id INTEGER NOT NULL DEFAULT 0,
                    source TEXT NOT NULL DEFAULT 'system',
                    session_hash TEXT NOT NULL DEFAULT '',
                    event_hash TEXT NOT NULL DEFAULT '',
                    from_stage TEXT NOT NULL DEFAULT '',
                    to_stage TEXT NOT NULL DEFAULT '',
                    elapsed_seconds INTEGER NOT NULL DEFAULT 0,
                    details_json TEXT NOT NULL DEFAULT '{}'
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS xinchao_deliveries (
                    delivery_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    cycle_id INTEGER NOT NULL,
                    delivered_at TEXT NOT NULL,
                    elapsed_seconds INTEGER NOT NULL,
                    state_json TEXT NOT NULL,
                    dominant TEXT NOT NULL,
                    monologue TEXT NOT NULL DEFAULT '',
                    UNIQUE(cycle_id)
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS xinchao_darkflow (
                    slot_id INTEGER PRIMARY KEY CHECK (slot_id = 1),
                    cycle_id INTEGER NOT NULL,
                    created_at TEXT NOT NULL,
                    content TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending',
                    delivered_at TEXT,
                    mailbox_message_id INTEGER,
                    mailbox_created_at TEXT,
                    event_count INTEGER NOT NULL DEFAULT 0,
                    context_json TEXT NOT NULL DEFAULT '[]',
                    CHECK (status IN ('pending', 'delivered'))
                )
                """
            )
            darkflow_columns = {
                row[1]
                for row in connection.execute("PRAGMA table_info(xinchao_darkflow)").fetchall()
            }
            darkflow_migrations = {
                "absence_started_at": "TEXT",
                "elapsed_seconds": "INTEGER NOT NULL DEFAULT 0",
                "stage_index": "INTEGER NOT NULL DEFAULT 0",
                "sleep_stage": "TEXT NOT NULL DEFAULT 'awake'",
                "next_stage_at": "TEXT",
                "revision": "INTEGER NOT NULL DEFAULT 1",
                "aftereffect_json": "TEXT NOT NULL DEFAULT '{}'",
                "aftereffect_applied_at": "TEXT",
                "memory_resonance_json": "TEXT NOT NULL DEFAULT '[]'",
            }
            for column, declaration in darkflow_migrations.items():
                if column not in darkflow_columns:
                    add_column("xinchao_darkflow", column, declaration)
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_xinchao_event_fingerprint "
                "ON xinchao_events(fingerprint, created_at DESC)"
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_xinchao_event_status "
                "ON xinchao_events(status, event_id)"
            )
            connection.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_xinchao_external_event "
                "ON xinchao_events(external_event_hash) "
                "WHERE external_event_hash IS NOT NULL AND external_event_hash<>''"
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_xinchao_transition_time "
                "ON xinchao_transitions(created_at DESC)"
            )
            connection.execute(
                "INSERT OR IGNORE INTO xinchao_state "
                "(state_id, pipes_json, updated_at) VALUES (1, ?, ?)",
                (json.dumps(self._baseline_floors(), ensure_ascii=False), now_iso()),
            )

    @staticmethod
    def _fingerprint(content: str) -> str:
        text = re.sub(r"(?m)^【\d{4}-\d{2}-\d{2}】\s*", "", str(content))
        text = re.sub(r"(?m)^--- \d{4}-\d{2}-\d{2}T\d{2}:\d{2} ---\s*", "", text)
        text = re.sub(r"\s+", "", text).casefold()
        return hashlib.sha256(text.encode("utf-8")).hexdigest()

    @staticmethod
    def _canonical_tag(value: str) -> str:
        return re.sub(r"[^\w\u4e00-\u9fff]+", "", str(value).casefold())[:80] or "事件"

    @staticmethod
    def _opaque_hash(value: str, length: int = 24) -> str:
        clean = str(value or "").strip()
        if not clean:
            return ""
        return hashlib.sha256(clean.encode("utf-8")).hexdigest()[:length]

    @staticmethod
    def _journal_sync(
        connection: sqlite3.Connection,
        transition_type: str,
        *,
        cycle_id: int = 0,
        source: str = "system",
        session_hash: str = "",
        event_hash: str = "",
        from_stage: str = "",
        to_stage: str = "",
        elapsed_seconds: int = 0,
        details: dict | None = None,
    ) -> None:
        safe_details = {}
        for key, value in (details or {}).items():
            if isinstance(value, bool):
                safe_details[str(key)[:60]] = value
            elif isinstance(value, (int, float)):
                safe_details[str(key)[:60]] = value
            elif isinstance(value, str):
                safe_details[str(key)[:60]] = value[:80]
        connection.execute(
            """
            INSERT INTO xinchao_transitions (
                created_at, transition_type, cycle_id, source,
                session_hash, event_hash, from_stage, to_stage,
                elapsed_seconds, details_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                now_iso(),
                str(transition_type)[:80],
                int(cycle_id),
                str(source)[:80],
                str(session_hash)[:32],
                str(event_hash)[:32],
                str(from_stage)[:40],
                str(to_stage)[:40],
                max(0, int(elapsed_seconds)),
                json.dumps(safe_details, ensure_ascii=False, sort_keys=True),
            ),
        )

    def _queue_sync(
        self,
        content: str,
        source_tool: str,
        source_ref: str,
        external_event_id: str = "",
    ) -> dict:
        timestamp = now_iso()
        fingerprint = self._fingerprint(content)
        external_event_hash = self._opaque_hash(external_event_id)
        cutoff = (beijing_now() - timedelta(hours=self.exact_dedupe_hours)).isoformat(
            timespec="seconds"
        )
        with self._connect() as connection:
            if external_event_hash:
                duplicate = connection.execute(
                    "SELECT event_id FROM xinchao_events WHERE external_event_hash=? LIMIT 1",
                    (external_event_hash,),
                ).fetchone()
                if duplicate:
                    return {
                        "status": "duplicate",
                        "event_id": int(duplicate["event_id"]),
                        "reason": "event_id",
                    }
            duplicate = connection.execute(
                """
                SELECT event_id FROM xinchao_events
                WHERE fingerprint = ? AND created_at >= ?
                  AND status IN ('pending', 'processing', 'applied', 'duplicate')
                ORDER BY event_id DESC LIMIT 1
                """,
                (fingerprint, cutoff),
            ).fetchone()
            if duplicate:
                return {"status": "duplicate", "event_id": int(duplicate["event_id"])}
            cursor = connection.execute(
                """
                INSERT INTO xinchao_events (
                    created_at, source_tool, source_ref, fingerprint, content,
                    prompt_hash, external_event_hash
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    timestamp,
                    str(source_tool)[:80],
                    str(source_ref or "")[:160],
                    fingerprint,
                    str(content),
                    self.evaluator.prompt_hash,
                    external_event_hash or None,
                ),
            )
            return {"status": "pending", "event_id": int(cursor.lastrowid)}

    def _event_sync(self, event_id: int) -> dict | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM xinchao_events WHERE event_id = ?", (int(event_id),)
            ).fetchone()
        return dict(row) if row else None

    def _mark_error_sync(self, event_id: int, error: str) -> None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT attempt_count FROM xinchao_events WHERE event_id=?",
                (int(event_id),),
            ).fetchone()
            attempts = int(row["attempt_count"] if row else 0) + 1
            delay_minutes = (5, 30, 120)[min(attempts - 1, 2)]
            retry_at = (beijing_now() + timedelta(minutes=delay_minutes)).isoformat(
                timespec="seconds"
            )
            connection.execute(
                """
                UPDATE xinchao_events SET status='pending', error=?,
                    attempt_count=?, next_retry_at=? WHERE event_id=?
                """,
                (str(error)[:500], attempts, retry_at, int(event_id)),
            )

    def _pending_ids_sync(self, exclude_event_id: int = 0, limit: int = 3) -> list[int]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT event_id FROM xinchao_events
                WHERE status='pending' AND event_id<>?
                  AND (next_retry_at IS NULL OR next_retry_at<=?)
                ORDER BY event_id ASC LIMIT ?
                """,
                (int(exclude_event_id), now_iso(), max(1, int(limit))),
            ).fetchall()
        return [int(row["event_id"]) for row in rows]

    @staticmethod
    def _active_thoughts_sync(
        connection: sqlite3.Connection, moment: datetime
    ) -> list[dict]:
        rows = connection.execute(
            "SELECT * FROM xinchao_thoughts WHERE expires_at > ? ORDER BY last_seen DESC",
            (moment.isoformat(timespec="seconds"),),
        ).fetchall()
        return [dict(row) for row in rows]

    @classmethod
    def _floors_from_thoughts(cls, thoughts: list[dict]) -> dict[str, float]:
        floors: dict[str, float] = {}
        for thought in thoughts:
            if thought.get("status") != "obsession":
                continue
            try:
                values = json.loads(thought.get("floor_json") or "{}")
            except (TypeError, ValueError):
                continue
            for name, raw_value in values.items():
                if name in PIPE_NAMES:
                    floors[name] = max(floors.get(name, 0.0), float(raw_value))
        return floors

    def _baseline_floors(self) -> dict[str, float]:
        private = {}
        reader = getattr(self.evaluator, "read_judge_config", None)
        if callable(reader):
            try:
                private = reader().get("baselines", {})
            except (OSError, TypeError, ValueError):
                logger.warning("Xinchao baseline config unavailable; using defaults")
        return self.engine.baseline_pipes(private)

    def _combined_floors(self, thoughts: list[dict]) -> dict[str, float]:
        floors = self._baseline_floors()
        for name, value in self._floors_from_thoughts(thoughts).items():
            floors[name] = max(floors.get(name, 0.0), float(value))
        return floors

    def _update_thought_sync(
        self,
        connection: sqlite3.Connection,
        canonical_tag: str,
        event_tag: str,
        deltas: dict,
        moment: datetime,
        *,
        thought_text: str = "",
        tone: str = "mixed",
        intensity: float = 0.3,
        reason: str = "",
        source_event_id: int | None = None,
        source_tool: str = "",
        source_ref: str = "",
    ) -> None:
        row = connection.execute(
            "SELECT * FROM xinchao_thoughts WHERE canonical_tag=?", (canonical_tag,)
        ).fetchone()
        active = row and parse_timestamp(row["expires_at"]) > moment
        count = int(row["occurrence_count"]) + 1 if active else 1
        status = "obsession" if count >= self.obsession_repeats else "flash"
        lifetime = self.obsession_hours if status == "obsession" else self.flash_hours
        floor = {}
        if status == "obsession":
            for name, raw_value in deltas.items():
                try:
                    value = float(raw_value)
                except (TypeError, ValueError):
                    continue
                if value > 0:
                    limit = 0.05 if name in {"难过", "生气", "醋", "自省"} else 0.08
                    floor[name] = round(min(limit, value * 0.15), 4)
        first_seen = row["first_seen"] if active else moment.isoformat(timespec="seconds")
        expires_at = (moment + timedelta(hours=lifetime)).isoformat(timespec="seconds")
        connection.execute(
            """
            INSERT INTO xinchao_thoughts (
                canonical_tag, event_tag, first_seen, last_seen,
                occurrence_count, status, floor_json, expires_at,
                thought_text, tone, intensity, reason, source_event_id,
                source_tool, source_ref, privacy, resolved_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'inner_only', NULL, ?)
            ON CONFLICT(canonical_tag) DO UPDATE SET
                event_tag=excluded.event_tag,
                first_seen=excluded.first_seen,
                last_seen=excluded.last_seen,
                occurrence_count=excluded.occurrence_count,
                status=excluded.status,
                floor_json=excluded.floor_json,
                expires_at=excluded.expires_at,
                thought_text=CASE WHEN excluded.thought_text<>'' THEN excluded.thought_text ELSE xinchao_thoughts.thought_text END,
                tone=excluded.tone,
                intensity=MAX(xinchao_thoughts.intensity, excluded.intensity),
                reason=CASE WHEN excluded.reason<>'' THEN excluded.reason ELSE xinchao_thoughts.reason END,
                source_event_id=excluded.source_event_id,
                source_tool=excluded.source_tool,
                source_ref=excluded.source_ref,
                privacy='inner_only',
                resolved_at=NULL,
                updated_at=excluded.updated_at
            """,
            (
                canonical_tag,
                event_tag,
                first_seen,
                moment.isoformat(timespec="seconds"),
                count,
                status,
                json.dumps(floor, ensure_ascii=False),
                expires_at,
                str(thought_text).strip()[:240],
                tone if tone in {"positive", "negative", "mixed"} else "mixed",
                max(0.0, min(1.0, float(intensity))),
                str(reason).strip()[:240],
                source_event_id,
                str(source_tool)[:80],
                str(source_ref)[:160],
                moment.isoformat(timespec="seconds"),
            ),
        )

    def _apply_sync(self, event_id: int, evaluation: dict) -> dict:
        processed_at = now_iso()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            event = connection.execute(
                "SELECT * FROM xinchao_events WHERE event_id=?", (int(event_id),)
            ).fetchone()
            if not event or event["status"] == "applied":
                return {"status": "unchanged", "event_id": event_id}
            moment = parse_timestamp(event["created_at"])
            canonical_tag = self._canonical_tag(evaluation.get("event_tag", ""))
            cutoff = (moment - timedelta(seconds=self.paraphrase_dedupe_seconds)).isoformat(
                timespec="seconds"
            )
            duplicate = connection.execute(
                """
                SELECT event_id FROM xinchao_events
                WHERE canonical_tag=? AND created_at>=? AND event_id<>?
                  AND status='applied'
                ORDER BY event_id DESC LIMIT 1
                """,
                (canonical_tag, cutoff, int(event_id)),
            ).fetchone()
            if duplicate:
                connection.execute(
                    """
                    UPDATE xinchao_events SET status='duplicate', content=NULL,
                        event_summary=?, event_tag=?, canonical_tag=?, processed_at=?
                    WHERE event_id=?
                    """,
                    (
                        evaluation["event"],
                        evaluation["event_tag"],
                        canonical_tag,
                        processed_at,
                        int(event_id),
                    ),
                )
                return {"status": "duplicate", "event_id": event_id}

            state = connection.execute(
                "SELECT * FROM xinchao_state WHERE state_id=1"
            ).fetchone()
            previous_stage = str(state["sleep_stage"] or "awake")
            cycle_id = int(state["cycle_id"])
            if state["cycle_open"]:
                pipes = json.loads(state["pipes_json"])
                previous = parse_timestamp(state["last_event_at"])
                if moment > previous:
                    thoughts = self._active_thoughts_sync(connection, moment)
                    floors = self._combined_floors(thoughts)
                    pipes = self.engine.evolve(pipes, previous, moment, floors)
            else:
                cycle_id += 1
                pipes = self._baseline_floors()

            private_thoughts = evaluation.get("inner_thoughts") or []
            if private_thoughts:
                for item in private_thoughts[:2]:
                    thought_tag = self._canonical_tag(item.get("tag") or item.get("text"))
                    self._update_thought_sync(
                        connection,
                        thought_tag,
                        str(item.get("tag") or evaluation["event_tag"]),
                        evaluation.get("pipes", {}),
                        moment,
                        thought_text=str(item.get("text", "")),
                        tone=str(item.get("tone", "mixed")),
                        intensity=float(item.get("intensity", 0.3)),
                        reason=str(item.get("reason", "")),
                        source_event_id=int(event_id),
                        source_tool=str(event["source_tool"]),
                        source_ref=str(event["source_ref"]),
                    )
            else:
                # Preserve the old repeat counter for compatibility, but legacy
                # event tags stay hidden from the new private-thought page.
                self._update_thought_sync(
                    connection,
                    canonical_tag,
                    evaluation["event_tag"],
                    evaluation.get("pipes", {}),
                    moment,
                    source_event_id=int(event_id),
                    source_tool=str(event["source_tool"]),
                    source_ref=str(event["source_ref"]),
                )
            thoughts = self._active_thoughts_sync(connection, moment)
            floors = self._combined_floors(thoughts)
            pipes = self.engine.apply_event(pipes, evaluation.get("pipes", {}), floors)
            connection.execute(
                """
                UPDATE xinchao_state SET cycle_id=?, cycle_open=1,
                    last_event_at=?, pipes_json=?, last_event_summary=?,
                    last_event_tag=?, last_presence_at=?, cycle_origin='event',
                    sleep_stage='awake',
                    sleep_started_at=NULL, deep_sleep_at=NULL,
                    darkflow_stage=0, last_darkflow_at=NULL,
                    darkflow_retry_at=NULL, darkflow_failures=0,
                    updated_at=?, version=version+1
                WHERE state_id=1
                """,
                (
                    cycle_id,
                    moment.isoformat(timespec="seconds"),
                    json.dumps(pipes, ensure_ascii=False),
                    evaluation["event"],
                    evaluation["event_tag"],
                    moment.isoformat(timespec="seconds"),
                    processed_at,
                ),
            )
            connection.execute(
                """
                UPDATE xinchao_events SET content=NULL, event_summary=?, event_tag=?,
                    context_card=?, cycle_id=?, canonical_tag=?, severity=?,
                    deltas_json=?, narrative_complete=?,
                    quality_note=?, status='applied', error='', processed_at=?
                WHERE event_id=?
                """,
                (
                    evaluation["event"],
                    evaluation["event_tag"],
                    evaluation.get("context_card", evaluation["event"]),
                    cycle_id,
                    canonical_tag,
                    float(evaluation["severity"]),
                    json.dumps(evaluation.get("pipes", {}), ensure_ascii=False),
                    int(bool(evaluation.get("narrative_complete", True))),
                    evaluation.get("quality_note", ""),
                    processed_at,
                    int(event_id),
                ),
            )
            self._journal_sync(
                connection,
                "narrative_event_applied",
                cycle_id=cycle_id,
                source=str(event["source_tool"]),
                event_hash=str(event["external_event_hash"] or ""),
                from_stage=previous_stage,
                to_stage="awake",
                details={
                    "changed_pipes": len(evaluation.get("pipes", {})),
                    "severity": float(evaluation.get("severity", 0.0)),
                    "narrative_complete": bool(
                        evaluation.get("narrative_complete", True)
                    ),
                },
            )
        return {
            "status": "applied",
            "event_id": event_id,
            "cycle_id": cycle_id,
            "created_at": moment.isoformat(timespec="seconds"),
            "event_summary": evaluation["event"],
            "event_tag": evaluation["event_tag"],
            "context_card": evaluation.get("context_card", evaluation["event"]),
            "narrative_complete": evaluation.get("narrative_complete", True),
            "quality_note": evaluation.get("quality_note", ""),
        }

    async def _process_event(self, event_id: int) -> dict:
        event = await asyncio.to_thread(self._event_sync, event_id)
        if not event or event.get("status") not in ("pending", "processing"):
            return {"status": event.get("status", "missing") if event else "missing"}
        try:
            evaluation = await self.evaluator.evaluate(event.get("content") or "")
            return await asyncio.to_thread(self._apply_sync, event_id, evaluation)
        except Exception as error:
            logger.warning("Xinchao evaluation pending for event %s: %s", event_id, error)
            await asyncio.to_thread(self._mark_error_sync, event_id, str(error))
            return {"status": "pending", "event_id": event_id, "error": str(error)}

    async def record_event(
        self,
        content: str,
        source_tool: str,
        source_ref: str = "",
        external_event_id: str = "",
    ) -> dict:
        if not self.enabled:
            return {"status": "disabled"}
        text = str(content or "").strip()
        if not text:
            return {"status": "ignored"}
        queued = await asyncio.to_thread(
            self._queue_sync,
            text,
            source_tool,
            source_ref,
            external_event_id,
        )
        if queued["status"] == "duplicate":
            return queued
        async with self._process_lock:
            pending = await asyncio.to_thread(
                self._pending_ids_sync, queued["event_id"], 2
            )
            queued_result = None
            for event_id in pending:
                processed = await self._process_event(event_id)
                if int(event_id) == int(queued["event_id"]):
                    queued_result = processed
            return queued_result or await self._process_event(queued["event_id"])

    async def retry_pending(self, limit: int = 3) -> int:
        if not self.enabled:
            return 0
        processed = 0
        async with self._process_lock:
            pending = await asyncio.to_thread(self._pending_ids_sync, 0, limit)
            for event_id in pending:
                result = await self._process_event(event_id)
                if result.get("status") in ("applied", "duplicate"):
                    processed += 1
        return processed

    @staticmethod
    def _cycle_contexts_sync(
        connection: sqlite3.Connection, cycle_id: int, limit: int = 8
    ) -> list[dict]:
        rows = connection.execute(
            """
            SELECT event_id, created_at, source_tool, source_ref,
                   event_summary, event_tag, context_card
            FROM (
                SELECT event_id, created_at, source_tool, source_ref,
                       event_summary, event_tag, context_card
                FROM xinchao_events
                WHERE cycle_id=? AND status='applied'
                ORDER BY event_id DESC LIMIT ?
            )
            ORDER BY event_id ASC
            """,
            (int(cycle_id), max(1, min(20, int(limit)))),
        ).fetchall()
        return [dict(row) for row in rows]

    def _cycle_stage_hours(self, cycle_origin: str = "") -> list[float]:
        # Presence nudges and darkflow are separate timelines. A short silence
        # can prompt a Bark message, but it must never create a darkflow stage.
        return list(self.darkflow_stage_hours)

    def _target_stage(self, elapsed_seconds: int, cycle_origin: str = "") -> int:
        elapsed_hours = max(0.0, float(elapsed_seconds) / 3600.0)
        return sum(
            1
            for hour in self._cycle_stage_hours(cycle_origin)
            if elapsed_hours >= hour
        )

    def _sleep_stage(self, elapsed_seconds: int) -> str:
        hours = max(0.0, float(elapsed_seconds) / 3600.0)
        if hours >= self.deep_sleep_after_hours:
            return "hibernating"
        if hours >= 10:
            return "deep_sleep"
        if hours >= 8:
            return "dreaming"
        if hours >= 6:
            return "light_sleep"
        if hours >= self.drowsy_after_hours:
            return "drowsy"
        return "awake_waiting"

    def _next_stage_at(
        self,
        absence_started_at: datetime,
        stage_index: int,
        cycle_origin: str = "",
    ) -> str | None:
        stages = self._cycle_stage_hours(cycle_origin)
        if stage_index >= len(stages):
            return None
        return (
            absence_started_at
            + timedelta(hours=stages[stage_index])
        ).isoformat(timespec="seconds")

    def _preview_sync(self, moment: datetime) -> dict:
        with self._connect() as connection:
            state = connection.execute(
                "SELECT * FROM xinchao_state WHERE state_id=1"
            ).fetchone()
            thoughts = self._active_thoughts_sync(connection, moment)
            obsessions = [item for item in thoughts if item["status"] == "obsession"]
            floors = self._combined_floors(thoughts)
            if state["cycle_open"]:
                last_event = parse_timestamp(state["last_event_at"])
                presence = parse_timestamp(state["last_presence_at"] or last_event)
                absence_started = max(last_event, presence)
                pipes = self.engine.evolve(
                    json.loads(state["pipes_json"]),
                    last_event,
                    min(moment, absence_started),
                    floors,
                )
                if moment > absence_started:
                    pipes = self.engine.evolve_absence(
                        pipes,
                        absence_started,
                        moment,
                        floors,
                        drowsy_after_hours=self.drowsy_after_hours,
                        sleep_after_hours=self.sleep_after_hours,
                    )
                elapsed = max(0, int((moment - absence_started).total_seconds()))
                cycle_origin = str(state["cycle_origin"] or "event")
                interaction_phase = (
                    "silence"
                    if cycle_origin in {"presence", "acknowledgement"}
                    and elapsed < int(self.silence_to_absence_hours * 3600)
                    else "absence"
                )
                dominant, dominant_value = self.engine.dominant(pipes)
                return {
                    "available": True,
                    "repeated": False,
                    "cycle_id": int(state["cycle_id"]),
                    "version": int(state["version"]),
                    "last_event_at": state["last_event_at"],
                    "absence_started_at": absence_started.isoformat(timespec="seconds"),
                    "as_of": moment.isoformat(timespec="seconds"),
                    "elapsed_seconds": elapsed,
                    "since_event_seconds": max(
                        0, int((moment - last_event).total_seconds())
                    ),
                    "pipes": pipes,
                    "dominant": dominant,
                    "dominant_value": dominant_value,
                    "event_summary": state["last_event_summary"],
                    "cycle_origin": cycle_origin,
                    "interaction_phase": interaction_phase,
                    "silence_nudge_due": (
                        interaction_phase == "silence"
                        and elapsed >= int(self.presence_nudge_after_hours * 3600)
                    ),
                    "silence_to_absence_seconds": int(
                        self.silence_to_absence_hours * 3600
                    ),
                    "event_contexts": self._cycle_contexts_sync(
                        connection, int(state["cycle_id"])
                    ),
                    "obsessions": obsessions,
                    "thoughts": thoughts,
                    "sleep_stage": self._sleep_stage(elapsed),
                    "darkflow_stage": int(state["darkflow_stage"] or 0),
                    "darkflow_retry_at": state["darkflow_retry_at"],
                }
            # Deliveries are immutable audit snapshots. Once a cycle is consumed,
            # the current state must come from xinchao_state instead of replaying
            # the previous high-emotion snapshot on every later read.
            pipes = json.loads(state["pipes_json"])
            dominant, dominant_value = self.engine.dominant(pipes)
            return {
                "available": True,
                "repeated": True,
                "settled": True,
                "cycle_id": int(state["cycle_id"]),
                "version": int(state["version"]),
                "last_event_at": None,
                "as_of": moment.isoformat(timespec="seconds"),
                "elapsed_seconds": 0,
                "pipes": pipes,
                "dominant": dominant,
                "dominant_value": dominant_value,
                "event_summary": "",
                "obsessions": obsessions,
                "thoughts": thoughts,
                "sleep_stage": "awake",
                "darkflow_stage": 0,
            }

    @staticmethod
    def _clip_darkflow(value: str, max_chars: int) -> str:
        text = re.sub(r"\s+", " ", str(value or "")).strip()
        if len(text) <= max_chars:
            return text
        window = text[:max_chars]
        punctuation = max(window.rfind(mark) for mark in "。！？!?；;")
        if punctuation >= max(180, max_chars - 100):
            return window[: punctuation + 1].strip()
        return window.rstrip("，,、；;：:") + "…"

    def _save_darkflow_sync(
        self,
        preview: dict,
        moment: datetime,
        content: str,
        aftereffect: dict,
        stage_index: int,
        sleep_stage: str,
        next_stage_at: str | None,
        mailbox_context: dict | None,
    ) -> dict:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            state = connection.execute(
                "SELECT * FROM xinchao_state WHERE state_id=1"
            ).fetchone()
            if (
                not state["cycle_open"]
                or int(state["cycle_id"]) != int(preview["cycle_id"])
                or int(state["version"]) != int(preview["version"])
            ):
                return {"status": "stale"}
            mailbox = mailbox_context or {}
            event_contexts = preview.get("event_contexts", [])
            safe_aftereffect = {}
            remaining = 0.20
            for name, raw_value in (aftereffect or {}).items():
                if name not in PIPE_NAMES or remaining <= 0:
                    continue
                try:
                    value = max(-0.08, min(0.08, float(raw_value)))
                except (TypeError, ValueError):
                    continue
                value = max(-remaining, min(remaining, value))
                if abs(value) >= 0.001:
                    safe_aftereffect[name] = round(value, 4)
                    remaining -= abs(value)
            updated_pipes = self.engine.apply_event(
                preview["pipes"], safe_aftereffect, self._combined_floors(
                    self._active_thoughts_sync(connection, moment)
                )
            )
            connection.execute(
                """
                INSERT INTO xinchao_darkflow (
                    slot_id, cycle_id, created_at, content, status,
                    delivered_at, mailbox_message_id, mailbox_created_at,
                    event_count, context_json, absence_started_at,
                    elapsed_seconds, stage_index, sleep_stage, next_stage_at,
                    revision, aftereffect_json, aftereffect_applied_at,
                    memory_resonance_json
                ) VALUES (1, ?, ?, ?, 'pending', NULL, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?)
                ON CONFLICT(slot_id) DO UPDATE SET
                    cycle_id=excluded.cycle_id,
                    created_at=excluded.created_at,
                    content=excluded.content,
                    status='pending',
                    delivered_at=NULL,
                    mailbox_message_id=excluded.mailbox_message_id,
                    mailbox_created_at=excluded.mailbox_created_at,
                    event_count=excluded.event_count,
                    context_json=excluded.context_json,
                    absence_started_at=excluded.absence_started_at,
                    elapsed_seconds=excluded.elapsed_seconds,
                    stage_index=excluded.stage_index,
                    sleep_stage=excluded.sleep_stage,
                    next_stage_at=excluded.next_stage_at,
                    revision=xinchao_darkflow.revision+1
                    ,aftereffect_json=excluded.aftereffect_json
                    ,aftereffect_applied_at=excluded.aftereffect_applied_at
                    ,memory_resonance_json=excluded.memory_resonance_json
                """,
                (
                    int(preview["cycle_id"]),
                    moment.isoformat(timespec="seconds"),
                    content,
                    mailbox.get("message_id"),
                    mailbox.get("created_at"),
                    len(event_contexts),
                    json.dumps(event_contexts, ensure_ascii=False),
                    preview.get("absence_started_at"),
                    int(preview.get("elapsed_seconds", 0)),
                    int(stage_index),
                    sleep_stage,
                    next_stage_at,
                    json.dumps(safe_aftereffect, ensure_ascii=False),
                    moment.isoformat(timespec="seconds") if safe_aftereffect else None,
                    json.dumps(preview.get("memory_resonance", [])[:4], ensure_ascii=False),
                ),
            )
            previous_stage = str(state["sleep_stage"] or "awake")
            sleep_started_at = state["sleep_started_at"]
            if sleep_stage in {"light_sleep", "dreaming", "deep_sleep", "hibernating"}:
                sleep_started_at = sleep_started_at or moment.isoformat(timespec="seconds")
            deep_sleep_at = state["deep_sleep_at"]
            if sleep_stage == "hibernating":
                deep_sleep_at = deep_sleep_at or moment.isoformat(timespec="seconds")
            connection.execute(
                """
                UPDATE xinchao_state SET pipes_json=?, sleep_stage=?, sleep_started_at=?,
                    deep_sleep_at=?, darkflow_stage=?, last_darkflow_at=?,
                    darkflow_retry_at=NULL, darkflow_failures=0,
                    updated_at=?, version=version+1
                WHERE state_id=1
                """,
                (
                    json.dumps(updated_pipes, ensure_ascii=False),
                    sleep_stage,
                    sleep_started_at,
                    deep_sleep_at,
                    int(stage_index),
                    moment.isoformat(timespec="seconds"),
                    now_iso(),
                ),
            )
            self._journal_sync(
                connection,
                "darkflow_rewritten",
                cycle_id=int(preview["cycle_id"]),
                from_stage=previous_stage,
                to_stage=sleep_stage,
                elapsed_seconds=int(preview.get("elapsed_seconds", 0)),
                details={
                    "stage_index": int(stage_index),
                    "body_chars": len(content),
                    "event_count": len(event_contexts),
                    "has_mailbox": bool(mailbox_context),
                    "has_next_stage": bool(next_stage_at),
                    "aftereffect_count": len(safe_aftereffect),
                    "memory_resonance_count": len(
                        preview.get("memory_resonance", [])
                    ),
                    "memory_resonance_ids": ",".join(
                        str(item.get("bucket_id", ""))
                        for item in preview.get("memory_resonance", [])[:4]
                    ),
                },
            )
        return {"status": "updated", "stage_index": stage_index}

    def _darkflow_failure_sync(
        self, preview: dict, moment: datetime, stage_index: int, error: str
    ) -> dict:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            state = connection.execute(
                "SELECT * FROM xinchao_state WHERE state_id=1"
            ).fetchone()
            if (
                not state["cycle_open"]
                or int(state["cycle_id"]) != int(preview["cycle_id"])
                or int(state["version"]) != int(preview["version"])
            ):
                return {"status": "stale"}
            failures = int(state["darkflow_failures"] or 0) + 1
            skipped = failures >= 3
            retry_at = None if skipped else (
                moment + timedelta(minutes=30)
            ).isoformat(timespec="seconds")
            next_stage = int(stage_index) if skipped else int(state["darkflow_stage"] or 0)
            sleep_stage = self._sleep_stage(int(preview.get("elapsed_seconds", 0)))
            connection.execute(
                """
                UPDATE xinchao_state SET darkflow_stage=?, sleep_stage=?,
                    darkflow_failures=?, darkflow_retry_at=?, updated_at=?,
                    version=version+1 WHERE state_id=1
                """,
                (
                    next_stage,
                    sleep_stage,
                    0 if skipped else failures,
                    retry_at,
                    now_iso(),
                ),
            )
            self._journal_sync(
                connection,
                "darkflow_stage_skipped" if skipped else "darkflow_generation_failed",
                cycle_id=int(preview["cycle_id"]),
                to_stage=sleep_stage,
                elapsed_seconds=int(preview.get("elapsed_seconds", 0)),
                details={
                    "stage_index": int(stage_index),
                    "attempt": failures,
                    "error_type": str(error).split(":", 1)[0],
                },
            )
        return {"status": "skipped" if skipped else "retry", "attempt": failures}

    async def settle_darkflow(self, mailbox_context: dict | None = None) -> dict:
        """Advance the one-slot darkflow to the newest due absence stage."""
        if not self.enabled or not self.monologue_enabled:
            return {"status": "disabled"}
        await self.retry_pending(limit=3)
        async with self._process_lock:
            moment = beijing_now()
            preview = await asyncio.to_thread(self._preview_sync, moment)
            if not preview.get("available") or preview.get("repeated"):
                return {"status": "idle"}
            target_stage = self._target_stage(
                preview.get("elapsed_seconds", 0),
                preview.get("cycle_origin", ""),
            )
            current_stage = int(preview.get("darkflow_stage", 0))
            if preview.get("interaction_phase") == "silence":
                return {
                    "status": "waiting",
                    "stage_index": current_stage,
                    "phase": "silence",
                }
            if target_stage <= current_stage or target_stage <= 0:
                return {"status": "waiting", "stage_index": current_stage}
            retry_at = preview.get("darkflow_retry_at")
            if retry_at:
                try:
                    if parse_timestamp(retry_at) > moment:
                        return {"status": "backoff", "retry_at": retry_at}
                except (TypeError, ValueError):
                    pass

            existing = await asyncio.to_thread(self._darkflow_status_sync, False)
            if existing and int(existing.get("cycle_id", -1)) != int(preview["cycle_id"]):
                existing = None
            contexts = self._contexts_after_mailbox(
                preview.get("event_contexts", []), mailbox_context
            )
            presence_only = preview.get("cycle_origin") == "presence"
            if presence_only:
                contexts = []
                mailbox_context = None
            sleep_stage = self._sleep_stage(preview.get("elapsed_seconds", 0))
            next_stage_at = self._next_stage_at(
                parse_timestamp(preview["absence_started_at"]),
                target_stage,
                preview.get("cycle_origin", ""),
            )
            timing = {
                "absence_started_at": preview.get("absence_started_at"),
                "generated_at": moment.isoformat(timespec="seconds"),
                "elapsed": self.format_elapsed(preview.get("elapsed_seconds", 0)),
                "elapsed_seconds": int(preview.get("elapsed_seconds", 0)),
                "stage_index": target_stage,
                "sleep_stage": sleep_stage,
                "next_stage_at": next_stage_at,
                "deep_sleep_after_hours": self.deep_sleep_after_hours,
                "presence_only": presence_only,
                "interaction_phase": "absence",
            }
            timing["rhythm"] = await asyncio.to_thread(
                self._rhythm_sync,
                moment,
                int(preview.get("elapsed_seconds", 0)),
            )
            memory_resonance = []
            if self.memory_resonance_provider is not None:
                try:
                    memory_resonance = await self.memory_resonance_provider(
                        preview, contexts
                    )
                except Exception as error:
                    logger.warning("Memory resonance unavailable: %s", error)
            unresolved_tasks = []
            if self.task_context_provider is not None:
                try:
                    unresolved_tasks = await self.task_context_provider(
                        preview, contexts
                    )
                except Exception as error:
                    logger.warning("Task context unavailable: %s", error)
            try:
                private_thoughts = [
                    item
                    for item in (preview.get("thoughts") or [])
                    if str(item.get("thought_text") or "").strip()
                    and item.get("privacy") == "inner_only"
                ]
                private_thoughts.sort(
                    key=lambda item: (
                        item.get("status") == "obsession",
                        float(item.get("intensity") or 0.0),
                        str(item.get("last_seen") or ""),
                    ),
                    reverse=True,
                )
                generated = await self.evaluator.darkflow(
                    preview["pipes"],
                    contexts,
                    private_thoughts[:4],
                    mailbox_context,
                    previous_darkflow=(existing or {}).get("content", ""),
                    timing=timing,
                    memory_resonance=memory_resonance,
                    unresolved_tasks=unresolved_tasks,
                )
                if isinstance(generated, dict):
                    generated_text = generated.get("text", "")
                    aftereffect = generated.get("aftereffect", {})
                else:
                    generated_text = generated
                    aftereffect = {}
                content = self._clip_darkflow(generated_text, self.darkflow_max_chars)
                if not content:
                    raise ValueError("empty darkflow response")
            except Exception as error:
                logger.warning("Xinchao progressive darkflow failed: %s", error)
                return await asyncio.to_thread(
                    self._darkflow_failure_sync,
                    preview,
                    moment,
                    target_stage,
                    f"{error.__class__.__name__}: {error}",
                )
            return await asyncio.to_thread(
                self._save_darkflow_sync,
                {
                    **preview,
                    "event_contexts": contexts,
                    "memory_resonance": memory_resonance,
                },
                moment,
                content,
                aftereffect,
                target_stage,
                sleep_stage,
                next_stage_at,
                mailbox_context,
            )

    def _apply_behavior_feedback_sync(
        self, cycle_id: int, content: str, deltas: dict
    ) -> dict:
        safe_deltas = {}
        remaining = 0.10
        for name, raw_value in (deltas or {}).items():
            if name not in PIPE_NAMES or remaining <= 0:
                continue
            try:
                value = max(-0.05, min(0.05, float(raw_value)))
            except (TypeError, ValueError):
                continue
            value = max(-remaining, min(remaining, value))
            if abs(value) >= 0.001:
                safe_deltas[name] = round(value, 4)
                remaining -= abs(value)
        if not safe_deltas:
            return {"status": "ignored", "deltas": {}}

        moment = beijing_now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            state = connection.execute(
                "SELECT * FROM xinchao_state WHERE state_id=1"
            ).fetchone()
            if not state["cycle_open"] or int(state["cycle_id"]) != int(cycle_id):
                return {"status": "stale", "deltas": {}}
            thoughts = self._active_thoughts_sync(connection, moment)
            updated = self.engine.apply_event(
                json.loads(state["pipes_json"]),
                safe_deltas,
                self._combined_floors(thoughts),
            )
            connection.execute(
                """
                UPDATE xinchao_state SET pipes_json=?, updated_at=?, version=version+1
                WHERE state_id=1
                """,
                (json.dumps(updated, ensure_ascii=False), now_iso()),
            )
            self._journal_sync(
                connection,
                "behavior_feedback_applied",
                cycle_id=int(cycle_id),
                source="bark",
                event_hash=self._opaque_hash(content),
                details={
                    "changed_pipes": len(safe_deltas),
                    "positive_total": round(sum(v for v in safe_deltas.values() if v > 0), 4),
                    "negative_total": round(sum(v for v in safe_deltas.values() if v < 0), 4),
                },
            )
        return {"status": "applied", "deltas": safe_deltas}

    async def apply_behavior_feedback(
        self, cycle_id: int, content: str, deltas: dict
    ) -> dict:
        """Apply a bounded state aftereffect only after an outward send succeeds."""
        if not self.enabled:
            return {"status": "disabled", "deltas": {}}
        return await asyncio.to_thread(
            self._apply_behavior_feedback_sync, cycle_id, content, deltas
        )

    def _consume_sync(
        self,
        preview: dict,
        moment: datetime,
    ) -> dict | None:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            state = connection.execute(
                "SELECT * FROM xinchao_state WHERE state_id=1"
            ).fetchone()
            if (
                not state["cycle_open"]
                or int(state["cycle_id"]) != int(preview["cycle_id"])
                or int(state["version"]) != int(preview["version"])
            ):
                return None
            connection.execute(
                """
                INSERT OR REPLACE INTO xinchao_deliveries (
                    cycle_id, delivered_at, elapsed_seconds, state_json,
                    dominant, monologue
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    preview["cycle_id"],
                    moment.isoformat(timespec="seconds"),
                    preview["elapsed_seconds"],
                    json.dumps(preview["pipes"], ensure_ascii=False),
                    preview["dominant"],
                    "",
                ),
            )
            connection.execute(
                """
                UPDATE xinchao_state SET cycle_open=0, last_event_at=NULL,
                    pipes_json=?, last_presence_at=?, sleep_stage='awake',
                    sleep_started_at=NULL, deep_sleep_at=NULL,
                    darkflow_stage=0, last_darkflow_at=NULL,
                    darkflow_retry_at=NULL, darkflow_failures=0,
                    updated_at=?, version=version+1
                WHERE state_id=1
                """,
                (
                    json.dumps(self._baseline_floors(), ensure_ascii=False),
                    moment.isoformat(timespec="seconds"),
                    now_iso(),
                ),
            )
            self._journal_sync(
                connection,
                "boot_cycle_consumed",
                cycle_id=int(preview["cycle_id"]),
                from_stage=str(preview.get("sleep_stage", "")),
                to_stage="awake",
                elapsed_seconds=int(preview.get("elapsed_seconds", 0)),
                details={"had_darkflow": bool(preview.get("darkflow"))},
            )
        result = dict(preview)
        result["repeated"] = False
        return result

    def _darkflow_status_sync(self, pending_only: bool = False) -> dict | None:
        query = "SELECT * FROM xinchao_darkflow WHERE slot_id=1"
        if pending_only:
            query += " AND status='pending'"
        with self._connect() as connection:
            row = connection.execute(query).fetchone()
        if not row:
            return None
        result = dict(row)
        try:
            result["contexts"] = json.loads(result.pop("context_json", "[]"))
        except (TypeError, ValueError):
            result["contexts"] = []
        try:
            result["memory_resonance"] = json.loads(
                result.pop("memory_resonance_json", "[]")
            )
        except (TypeError, ValueError):
            result["memory_resonance"] = []
        return result

    async def darkflow_status(self) -> dict | None:
        """Return the one-slot darkflow without consuming it."""
        if not self.enabled:
            return None
        return await asyncio.to_thread(self._darkflow_status_sync, False)

    async def pending_darkflow(self) -> dict | None:
        if not self.enabled:
            return None
        return await asyncio.to_thread(self._darkflow_status_sync, True)

    def _mark_darkflow_delivered_sync(self, cycle_id: int) -> bool:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT stage_index, elapsed_seconds FROM xinchao_darkflow "
                "WHERE slot_id=1 AND cycle_id=? AND status='pending'",
                (int(cycle_id),),
            ).fetchone()
            cursor = connection.execute(
                """
                UPDATE xinchao_darkflow
                SET status='delivered', delivered_at=?
                WHERE slot_id=1 AND cycle_id=? AND status='pending'
                """,
                (now_iso(), int(cycle_id)),
            )
            if cursor.rowcount > 0:
                self._journal_sync(
                    connection,
                    "darkflow_delivered",
                    cycle_id=int(cycle_id),
                    elapsed_seconds=int(row["elapsed_seconds"] if row else 0),
                    details={"stage_index": int(row["stage_index"] if row else 0)},
                )
        return cursor.rowcount > 0

    async def mark_darkflow_delivered(self, cycle_id: int) -> bool:
        return await asyncio.to_thread(
            self._mark_darkflow_delivered_sync, cycle_id
        )

    def _acknowledge_seen_sync(self, moment: datetime) -> dict:
        """Partly satisfy response-related drives and restart silence."""
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            state = connection.execute(
                "SELECT * FROM xinchao_state WHERE state_id=1"
            ).fetchone()
            if not state:
                return {"status": "missing"}
            previous_cycle_id = int(state["cycle_id"])
            next_cycle_id = previous_cycle_id + 1
            stamp = moment.isoformat(timespec="seconds")
            thoughts = self._active_thoughts_sync(connection, moment)
            floors = self._combined_floors(thoughts)
            pipes = json.loads(state["pipes_json"])
            if state["cycle_open"] and state["last_event_at"]:
                try:
                    pipes = self.engine.evolve(
                        pipes,
                        parse_timestamp(state["last_event_at"]),
                        moment,
                        floors,
                    )
                except (TypeError, ValueError):
                    logger.warning(
                        "Could not evolve state before acknowledgement; using stored values"
                    )

            # Being seen eases the need for a response, but it does not erase
            # unrelated feelings or personality baselines.
            retain_excess = {
                "想知道她在干嘛": 0.55,
                "想靠近": 0.72,
                "想黏着": 0.68,
                "想分享": 0.80,
            }
            changed = {}
            for name, retention in retain_excess.items():
                floor = float(floors.get(name, 0.0))
                before = max(floor, float(pipes.get(name, 0.0)))
                after = floor + (before - floor) * retention
                pipes[name] = round(after, 6)
                changed[name] = round(after - before, 6)
            pipes = self.engine.apply_event(
                pipes,
                {"开心": 0.04, "满足": 0.06},
                floors,
            )
            pending_darkflow = connection.execute(
                "SELECT status FROM xinchao_darkflow "
                "WHERE slot_id=1 AND cycle_id=?",
                (previous_cycle_id,),
            ).fetchone()
            darkflow_carried = bool(
                pending_darkflow and pending_darkflow["status"] == "pending"
            )
            if darkflow_carried:
                # A push acknowledgement settles outward behavior, not inner handoff.
                connection.execute(
                    "UPDATE xinchao_darkflow SET cycle_id=? "
                    "WHERE slot_id=1 AND cycle_id=? AND status='pending'",
                    (next_cycle_id, previous_cycle_id),
                )
            else:
                connection.execute(
                    "DELETE FROM xinchao_darkflow "
                    "WHERE slot_id=1 AND cycle_id=?",
                    (previous_cycle_id,),
                )
            connection.execute(
                """
                UPDATE xinchao_state SET cycle_id=?, cycle_open=1,
                    last_event_at=?, last_presence_at=?, cycle_origin='acknowledgement',
                    last_event_summary='', last_event_tag='', pipes_json=?,
                    sleep_stage='awake', sleep_started_at=NULL, deep_sleep_at=NULL,
                    darkflow_stage=0, last_darkflow_at=NULL,
                    darkflow_retry_at=NULL, darkflow_failures=0,
                    updated_at=?, version=version+1
                WHERE state_id=1
                """,
                (
                    next_cycle_id,
                    stamp,
                    stamp,
                    json.dumps(pipes, ensure_ascii=False),
                    now_iso(),
                ),
            )
            self._journal_sync(
                connection,
                "behavior_acknowledged",
                cycle_id=next_cycle_id,
                source="manager",
                from_stage=str(state["sleep_stage"] or "awake"),
                to_stage="awake",
                details={
                    "partially_settled": True,
                    "changed_pipes": len(changed),
                    "positive_response": 0.10,
                    "pending_darkflow_carried": darkflow_carried,
                },
            )
        return {
            "status": "acknowledged",
            "previous_cycle_id": previous_cycle_id,
            "cycle_id": next_cycle_id,
            "silence_started_at": stamp,
            "pipes": pipes,
            "pending_darkflow_carried": darkflow_carried,
        }

    async def acknowledge_seen(self) -> dict:
        """Acknowledge an outward message without creating a memory event."""
        if not self.enabled:
            return {"status": "disabled"}
        return await asyncio.to_thread(
            self._acknowledge_seen_sync, beijing_now()
        )

    def _restart_silence_timer_sync(self, moment: datetime) -> dict:
        """Restart only the open-window silence clock; leave all inner state intact."""
        stamp = moment.isoformat(timespec="seconds")
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            state = connection.execute(
                "SELECT * FROM xinchao_state WHERE state_id=1"
            ).fetchone()
            if not state or not state["cycle_open"]:
                return {"status": "idle"}
            connection.execute(
                """
                UPDATE xinchao_state SET last_presence_at=?, sleep_stage='awake',
                    sleep_started_at=NULL, deep_sleep_at=NULL,
                    darkflow_stage=0, darkflow_retry_at=NULL,
                    darkflow_failures=0, updated_at=?, version=version+1
                WHERE state_id=1
                """,
                (stamp, now_iso()),
            )
            self._journal_sync(
                connection,
                "silence_timer_restarted",
                cycle_id=int(state["cycle_id"]),
                source="manager",
                from_stage=str(state["sleep_stage"] or "awake"),
                to_stage="awake",
                details={"timer_only": True},
            )
            pipes = json.loads(state["pipes_json"])
        return {
            "status": "restarted",
            "cycle_id": int(state["cycle_id"]),
            "silence_started_at": stamp,
            "pipes": pipes,
        }

    async def restart_silence_timer(self) -> dict:
        """Restart the open-window timer without touching hormones or thoughts."""
        if not self.enabled:
            return {"status": "disabled"}
        return await asyncio.to_thread(
            self._restart_silence_timer_sync, beijing_now()
        )

    def _observe_presence_sync(
        self,
        session_id: str,
        source: str,
        event_id: str,
        moment: datetime,
        start_cycle: bool,
    ) -> dict:
        session_hash = self._opaque_hash(session_id, 16)
        event_hash = self._opaque_hash(event_id, 16)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            state = connection.execute(
                "SELECT * FROM xinchao_state WHERE state_id=1"
            ).fetchone()
            if not state:
                return {"status": "missing"}
            previous_stage = str(state["sleep_stage"] or "awake")
            previous_presence = state["last_presence_at"] or state["last_event_at"]
            should_record_arrival = not previous_presence
            if previous_presence:
                try:
                    should_record_arrival = (
                        moment - parse_timestamp(previous_presence)
                    ).total_seconds() >= self.arrival_gap_minutes * 60
                except (TypeError, ValueError):
                    should_record_arrival = True
            if should_record_arrival:
                connection.execute(
                    "UPDATE xinchao_arrival_rhythm SET weight=weight*0.99"
                )
                connection.execute(
                    """
                    INSERT INTO xinchao_arrival_rhythm (hour, weight, sample_count, updated_at)
                    VALUES (?, 1.0, 1, ?)
                    ON CONFLICT(hour) DO UPDATE SET
                        weight=xinchao_arrival_rhythm.weight+1.0,
                        sample_count=xinchao_arrival_rhythm.sample_count+1,
                        updated_at=excluded.updated_at
                    """,
                    (int(moment.hour), moment.isoformat(timespec="seconds")),
                )
            started = False
            if state["cycle_open"]:
                connection.execute(
                    """
                    UPDATE xinchao_state SET last_presence_at=?, sleep_stage='awake',
                        sleep_started_at=NULL, deep_sleep_at=NULL,
                        darkflow_stage=0, darkflow_retry_at=NULL,
                        darkflow_failures=0, updated_at=?, version=version+1
                    WHERE state_id=1
                    """,
                    (moment.isoformat(timespec="seconds"), now_iso()),
                )
            else:
                if start_cycle:
                    started = True
                    cycle_id = int(state["cycle_id"]) + 1
                    connection.execute(
                        """
                        UPDATE xinchao_state SET cycle_id=?, cycle_open=1,
                            last_event_at=?, last_presence_at=?, cycle_origin='presence',
                            last_event_summary='', last_event_tag='', sleep_stage='awake',
                            sleep_started_at=NULL, deep_sleep_at=NULL,
                            darkflow_stage=0, last_darkflow_at=NULL,
                            darkflow_retry_at=NULL, darkflow_failures=0,
                            updated_at=?, version=version+1
                        WHERE state_id=1
                        """,
                        (
                            cycle_id,
                            moment.isoformat(timespec="seconds"),
                            moment.isoformat(timespec="seconds"),
                            now_iso(),
                        ),
                    )
                    self._journal_sync(
                        connection,
                        "presence_cycle_started",
                        cycle_id=cycle_id,
                        source=source,
                        session_hash=session_hash,
                        event_hash=event_hash,
                        to_stage="awake",
                    )
                else:
                    connection.execute(
                        "UPDATE xinchao_state SET last_presence_at=?, updated_at=? WHERE state_id=1",
                        (moment.isoformat(timespec="seconds"), now_iso()),
                    )
            if previous_stage not in {"awake", "awake_waiting"}:
                self._journal_sync(
                    connection,
                    "presence_wake",
                    cycle_id=int(state["cycle_id"]),
                    source=source,
                    session_hash=session_hash,
                    event_hash=event_hash,
                    from_stage=previous_stage,
                    to_stage="awake",
                )
        return {
            "status": "observed",
            "woke": previous_stage not in {"awake", "awake_waiting"},
            "cycle_started": started,
        }

    def _rhythm_sync(self, moment: datetime, elapsed_seconds: int = 0) -> dict:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT hour, weight, sample_count FROM xinchao_arrival_rhythm ORDER BY hour"
            ).fetchall()
        weights = {int(row["hour"]): float(row["weight"]) for row in rows}
        samples = sum(int(row["sample_count"]) for row in rows)
        maximum = max(weights.values(), default=0.0)
        nearby = (
            weights.get((moment.hour - 1) % 24, 0.0) * 0.35
            + weights.get(moment.hour, 0.0)
            + weights.get((moment.hour + 1) % 24, 0.0) * 0.65
        )
        activeness = 0.0 if maximum <= 0 else min(1.0, nearby / (maximum * 2.0))
        learned = samples >= self.rhythm_min_samples
        elapsed_hours = max(0.0, float(elapsed_seconds) / 3600.0)
        longing_progress = max(
            0.0,
            min(
                1.0,
                (elapsed_hours - self.longing_after_hours)
                / (self.longing_full_hours - self.longing_after_hours),
            ),
        )
        longing = longing_progress * activeness if learned and activeness >= 0.15 else 0.0
        return {
            "learned": learned,
            "sample_count": samples,
            "current_hour": int(moment.hour),
            "activeness": round(activeness, 4),
            "anticipation": round(activeness if learned else 0.0, 4),
            "longing": round(longing, 4),
            "hours": [
                {"hour": hour, "weight": round(weights.get(hour, 0.0), 4)}
                for hour in range(24)
            ],
        }

    async def rhythm_status(self) -> dict:
        state = await self.status()
        return await asyncio.to_thread(
            self._rhythm_sync,
            beijing_now(),
            int(state.get("elapsed_seconds", 0)),
        )

    async def observe_presence(
        self,
        session_id: str = "",
        source: str = "mcp",
        event_id: str = "",
        start_cycle: bool = False,
    ) -> dict:
        if not self.enabled:
            return {"status": "disabled"}
        return await asyncio.to_thread(
            self._observe_presence_sync,
            session_id,
            source,
            event_id,
            beijing_now(),
            bool(start_cycle),
        )

    def _boot_delivery_sync(self, session_id: str) -> dict | None:
        session_hash = self._opaque_hash(session_id, 24)
        if not session_hash:
            return None
        cutoff = (beijing_now() - timedelta(hours=self.boot_once_hours)).isoformat(
            timespec="seconds"
        )
        with self._connect() as connection:
            connection.execute(
                "DELETE FROM xinchao_boot_deliveries WHERE delivered_at<?", (cutoff,)
            )
            row = connection.execute(
                "SELECT * FROM xinchao_boot_deliveries WHERE session_hash=?",
                (session_hash,),
            ).fetchone()
        return dict(row) if row else None

    async def boot_delivery(self, session_id: str) -> dict | None:
        if not self.enabled or not session_id:
            return None
        return await asyncio.to_thread(self._boot_delivery_sync, session_id)

    def _record_boot_delivery_sync(self, session_id: str, body: str) -> None:
        session_hash = self._opaque_hash(session_id, 24)
        if not session_hash:
            return
        digest = hashlib.sha256(str(body).encode("utf-8")).hexdigest()[:16]
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO xinchao_boot_deliveries (
                    session_hash, delivered_at, body_digest, body_chars
                ) VALUES (?, ?, ?, ?)
                ON CONFLICT(session_hash) DO UPDATE SET
                    delivered_at=excluded.delivered_at,
                    body_digest=excluded.body_digest,
                    body_chars=excluded.body_chars
                """,
                (session_hash, now_iso(), digest, len(str(body))),
            )
            self._journal_sync(
                connection,
                "boot_context_delivered",
                source="pulse_boot",
                session_hash=session_hash[:16],
                details={"body_chars": len(str(body)), "digest": digest},
            )

    async def record_boot_delivery(self, session_id: str, body: str) -> None:
        if self.enabled and session_id:
            await asyncio.to_thread(self._record_boot_delivery_sync, session_id, body)

    def _recent_transitions_sync(self, limit: int = 50) -> list[dict]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM xinchao_transitions ORDER BY transition_id DESC LIMIT ?",
                (max(1, min(500, int(limit))),),
            ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            try:
                item["details"] = json.loads(item.pop("details_json", "{}"))
            except (TypeError, ValueError):
                item["details"] = {}
            result.append(item)
        return result

    async def recent_transitions(self, limit: int = 50) -> list[dict]:
        if not self.enabled:
            return []
        return await asyncio.to_thread(self._recent_transitions_sync, limit)

    async def status(self) -> dict:
        if not self.enabled:
            return {"available": False, "disabled": True}
        moment = beijing_now()
        preview = await asyncio.to_thread(self._preview_sync, moment)
        preview["rhythm"] = await asyncio.to_thread(
            self._rhythm_sync,
            moment,
            int(preview.get("elapsed_seconds", 0)),
        )
        return preview

    def _list_private_thoughts_sync(
        self, status: str = "active", limit: int = 100
    ) -> list[dict]:
        moment = beijing_now()
        params: list = []
        clauses = ["thought_text<>''", "privacy='inner_only'"]
        if status == "all":
            pass
        elif status == "flash":
            clauses.extend(["status='flash'", "resolved_at IS NULL", "expires_at>?"])
            params.append(moment.isoformat(timespec="seconds"))
        elif status == "obsession":
            clauses.extend(["status='obsession'", "resolved_at IS NULL", "expires_at>?"])
            params.append(moment.isoformat(timespec="seconds"))
        elif status == "resolved":
            clauses.append("resolved_at IS NOT NULL")
        elif status == "faded":
            clauses.extend(["resolved_at IS NULL", "expires_at<=?"])
            params.append(moment.isoformat(timespec="seconds"))
        else:
            clauses.extend(["resolved_at IS NULL", "expires_at>?"])
            params.append(moment.isoformat(timespec="seconds"))
        params.append(max(1, min(500, int(limit))))
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM xinchao_thoughts WHERE "
                + " AND ".join(clauses)
                + " ORDER BY intensity DESC, last_seen DESC LIMIT ?",
                params,
            ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            try:
                expires = parse_timestamp(item["expires_at"])
                lifetime = max(1.0, (expires - parse_timestamp(item["last_seen"])).total_seconds())
                remaining = max(0.0, (expires - moment).total_seconds())
                item["current_strength"] = round(
                    float(item.get("intensity", 0.3)) * min(1.0, remaining / lifetime), 4
                )
            except (TypeError, ValueError):
                item["current_strength"] = float(item.get("intensity", 0.3))
            item.pop("floor_json", None)
            item["private"] = True
            result.append(item)
        return result

    async def list_private_thoughts(
        self, status: str = "active", limit: int = 100
    ) -> list[dict]:
        if not self.enabled:
            return []
        return await asyncio.to_thread(self._list_private_thoughts_sync, status, limit)

    def _resolve_private_thought_sync(self, canonical_tag: str) -> bool:
        with self._connect() as connection:
            cursor = connection.execute(
                "UPDATE xinchao_thoughts SET resolved_at=?, status='resolved', "
                "updated_at=? WHERE canonical_tag=? AND thought_text<>''",
                (now_iso(), now_iso(), str(canonical_tag)),
            )
        return cursor.rowcount > 0

    async def resolve_private_thought(self, canonical_tag: str) -> bool:
        return await asyncio.to_thread(self._resolve_private_thought_sync, canonical_tag)

    def _delete_private_thought_sync(self, canonical_tag: str) -> bool:
        with self._connect() as connection:
            cursor = connection.execute(
                "DELETE FROM xinchao_thoughts WHERE canonical_tag=? AND thought_text<>''",
                (str(canonical_tag),),
            )
        return cursor.rowcount > 0

    async def delete_private_thought(self, canonical_tag: str) -> bool:
        return await asyncio.to_thread(self._delete_private_thought_sync, canonical_tag)

    @staticmethod
    def _contexts_after_mailbox(
        contexts: list[dict], mailbox_context: dict | None
    ) -> list[dict]:
        if not mailbox_context or not mailbox_context.get("created_at"):
            return list(contexts)
        try:
            mailbox_time = parse_timestamp(mailbox_context["created_at"])
        except (TypeError, ValueError):
            return list(contexts)
        result = []
        for item in contexts:
            try:
                event_time = parse_timestamp(item.get("created_at", ""))
            except (TypeError, ValueError):
                continue
            if event_time > mailbox_time:
                result.append(item)
        return result

    async def consume_boot(self, mailbox_context: dict | None = None) -> dict:
        if not self.enabled:
            return {"available": False, "disabled": True}
        await self.settle_darkflow(mailbox_context=mailbox_context)
        for _ in range(2):
            moment = beijing_now()
            preview = await asyncio.to_thread(self._preview_sync, moment)
            if not preview.get("available") or preview.get("repeated"):
                return preview
            darkflow = await self.pending_darkflow()
            legacy_early_darkflow = bool(
                darkflow
                and preview.get("interaction_phase") == "silence"
                and int(darkflow.get("elapsed_seconds", 0))
                < int(self.silence_to_absence_hours * 3600)
            )
            if (
                darkflow
                and not legacy_early_darkflow
                and int(darkflow.get("cycle_id", -1)) == int(preview["cycle_id"])
            ):
                preview["darkflow"] = darkflow.get("content", "")
                preview["darkflow_item"] = darkflow
            else:
                preview["darkflow"] = ""
                preview["darkflow_item"] = None
            consumed = await asyncio.to_thread(
                self._consume_sync,
                preview,
                moment,
            )
            if consumed is not None:
                return consumed
        return await self.status()

    @staticmethod
    def format_elapsed(seconds: int) -> str:
        total = max(0, int(seconds))
        hours, remainder = divmod(total, 3600)
        minutes = remainder // 60
        return f"{hours}h{minutes:02d}m"

    @staticmethod
    def format_elapsed_zh(seconds: int) -> str:
        total = max(0, int(seconds))
        hours, remainder = divmod(total, 3600)
        minutes = remainder // 60
        if hours:
            return f"{hours}小时{minutes}分钟"
        return f"{minutes}分钟"

    @classmethod
    def render_compact(cls, state: dict) -> str:
        if state.get("disabled"):
            return "激素未启用。"
        if not state.get("available"):
            return "激素尚未开始：下一次叙事记忆写入后开始计时。"
        pipes = state.get("pipes", {})
        highlights = sorted(pipes.items(), key=lambda item: item[1], reverse=True)[:4]
        values = " | ".join(f"{name} {value:.2f}" for name, value in highlights)
        repeated = "（本轮已交付，当前为基础状态）" if state.get("settled") else (
            "（本窗口重复握手，沿用同一份交接）" if state.get("repeated") else ""
        )
        stage_labels = {
            "awake": "清醒",
            "awake_waiting": "清醒等待",
            "drowsy": "困倦",
            "light_sleep": "浅睡",
            "dreaming": "梦境沉淀",
            "deep_sleep": "深睡",
            "hibernating": "深度休眠",
        }
        stage = stage_labels.get(
            str(state.get("sleep_stage", "awake")),
            str(state.get("sleep_stage", "awake")),
        )
        lines = [
            f"离开计时起点（UTC+8）：{state.get('absence_started_at') or '本轮已交付'}",
            f"状态时间（UTC+8）：{state.get('as_of') or now_iso()}",
            f"已经过 {cls.format_elapsed(state.get('elapsed_seconds', 0))}{repeated}｜阶段：{stage}",
            f"主导：{state.get('dominant', '无')} {state.get('dominant_value', 0.0):.2f}",
            values,
        ]
        return "\n".join(line for line in lines if line)

    @classmethod
    def render_full(cls, state: dict) -> str:
        if not state.get("available"):
            return cls.render_compact(state)
        lines = ["=== 激素状态 ===", cls.render_compact(state), "", "【全部状态】"]
        for name in PIPE_NAMES:
            lines.append(f"{name}: {float(state.get('pipes', {}).get(name, 0.0)):.3f}")
        lines.append("说明：这是沉默期间的状态快照，只影响表达，不命令行为。")
        return "\n".join(lines)
