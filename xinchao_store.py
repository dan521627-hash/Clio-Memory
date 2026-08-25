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
import uuid
from datetime import datetime, timedelta

from rapidfuzz import fuzz

from utils import beijing_now, now_iso
from xinchao_engine import (
    PIPE_NAMES,
    XinchaoEngine,
    empty_pipes,
    infer_composite_states,
    parse_timestamp,
    pipe_catalog,
)
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
        self.thought_feed_limit = max(2, int(settings.get("thought_feed_limit", 6)))
        self.satisfaction_plateau_hours = max(
            0.25, float(settings.get("satisfaction_plateau_hours", 2.0))
        )
        self.monologue_enabled = bool(settings.get("monologue_enabled", True))
        self.monologue_after_hours = max(
            0.0, float(settings.get("monologue_after_hours", 1.0))
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
                float(settings.get("silence_to_absence_minutes", 30)) / 60.0,
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
        self.thought_embedding_provider = None
        self._process_lock = asyncio.Lock()
        if self.enabled:
            os.makedirs(os.path.dirname(os.path.abspath(self.db_path)), exist_ok=True)
            self._initialize()

    def set_memory_resonance_provider(self, provider) -> None:
        self.memory_resonance_provider = provider

    def set_task_context_provider(self, provider) -> None:
        self.task_context_provider = provider

    def set_thought_embedding_provider(self, provider) -> None:
        """Attach the existing semantic index for private-thought search."""
        self.thought_embedding_provider = provider

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
                "pipes_updated_at": "TEXT",
                "last_presence_at": "TEXT",
                "cycle_origin": "TEXT NOT NULL DEFAULT 'event'",
                "sleep_stage": "TEXT NOT NULL DEFAULT 'awake'",
                "sleep_started_at": "TEXT",
                "deep_sleep_at": "TEXT",
                "darkflow_stage": "INTEGER NOT NULL DEFAULT 0",
                "last_darkflow_at": "TEXT",
                "darkflow_retry_at": "TEXT",
                "darkflow_failures": "INTEGER NOT NULL DEFAULT 0",
                "static_ready": "INTEGER NOT NULL DEFAULT 0",
                "static_started_at": "TEXT",
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
            if "handoff_ready" not in event_columns:
                add_column(
                    "xinchao_events", "handoff_ready", "INTEGER NOT NULL DEFAULT 0"
                )
            if "cycle_id" not in event_columns:
                add_column(
                    "xinchao_events", "cycle_id", "INTEGER NOT NULL DEFAULT 0"
                )
            if "external_event_hash" not in event_columns:
                add_column("xinchao_events", "external_event_hash", "TEXT")
            if "correction_key_hash" not in event_columns:
                add_column("xinchao_events", "correction_key_hash", "TEXT")
            if "supersedes_event_id" not in event_columns:
                add_column("xinchao_events", "supersedes_event_id", "INTEGER")
            if "superseded_by_event_id" not in event_columns:
                add_column("xinchao_events", "superseded_by_event_id", "INTEGER")
            if "signals_json" not in event_columns:
                add_column(
                    "xinchao_events", "signals_json", "TEXT NOT NULL DEFAULT '[]'"
                )
            if "composites_json" not in event_columns:
                add_column(
                    "xinchao_events", "composites_json", "TEXT NOT NULL DEFAULT '[]'"
                )
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
                "thought_kind": "TEXT NOT NULL DEFAULT 'inner'",
                "thought_text": "TEXT NOT NULL DEFAULT ''",
                "linkage_json": "TEXT NOT NULL DEFAULT '{}'",
                "tone": "TEXT NOT NULL DEFAULT 'mixed'",
                "intensity": "REAL NOT NULL DEFAULT 0.3",
                "reason": "TEXT NOT NULL DEFAULT ''",
                "source_event_id": "INTEGER",
                "source_tool": "TEXT NOT NULL DEFAULT ''",
                "source_ref": "TEXT NOT NULL DEFAULT ''",
                "privacy": "TEXT NOT NULL DEFAULT 'inner_only'",
                "resolved_at": "TEXT",
                "updated_at": "TEXT",
                "feed_count": "INTEGER NOT NULL DEFAULT 0",
                "last_fed_at": "TEXT",
                "retired_at": "TEXT",
            }
            for column, declaration in thought_migrations.items():
                if column not in thought_columns:
                    add_column("xinchao_thoughts", column, declaration)
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS xinchao_trace_revisions (
                    revision_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    canonical_tag TEXT NOT NULL,
                    revised_at TEXT NOT NULL,
                    old_text TEXT NOT NULL,
                    new_text TEXT NOT NULL,
                    old_linkage_json TEXT NOT NULL DEFAULT '{}',
                    new_linkage_json TEXT NOT NULL DEFAULT '{}',
                    source_tool TEXT NOT NULL DEFAULT '',
                    source_ref TEXT NOT NULL DEFAULT ''
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS xinchao_thought_contributions (
                    source_event_id INTEGER NOT NULL,
                    canonical_tag TEXT NOT NULL,
                    event_tag TEXT NOT NULL DEFAULT '',
                    thought_kind TEXT NOT NULL DEFAULT 'inner',
                    thought_text TEXT NOT NULL DEFAULT '',
                    tone TEXT NOT NULL DEFAULT 'mixed',
                    intensity REAL NOT NULL DEFAULT 0.3,
                    reason TEXT NOT NULL DEFAULT '',
                    source_tool TEXT NOT NULL DEFAULT '',
                    source_ref TEXT NOT NULL DEFAULT '',
                    linkage_json TEXT NOT NULL DEFAULT '{}',
                    deltas_json TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL,
                    reverted_at TEXT,
                    PRIMARY KEY (source_event_id, canonical_tag)
                )
                """
            )
            # Existing databases only knew the latest source event for a
            # merged thought. Preserve that known contribution so future
            # corrections are still reversible from this migration onward.
            connection.execute(
                """
                INSERT OR IGNORE INTO xinchao_thought_contributions (
                    source_event_id, canonical_tag, event_tag, thought_kind,
                    thought_text, tone, intensity, reason, source_tool,
                    source_ref, linkage_json, deltas_json, created_at
                )
                SELECT source_event_id, canonical_tag, event_tag, thought_kind,
                       thought_text, tone, intensity, reason, source_tool,
                       source_ref, linkage_json, '{}',
                       COALESCE(last_fed_at, updated_at, last_seen)
                FROM xinchao_thoughts
                WHERE source_event_id IS NOT NULL
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS xinchao_plateaus (
                    pipe_name TEXT PRIMARY KEY,
                    until_at TEXT NOT NULL,
                    source TEXT NOT NULL DEFAULT '',
                    updated_at TEXT NOT NULL
                )
                """
            )
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
                "CREATE INDEX IF NOT EXISTS idx_xinchao_event_correction "
                "ON xinchao_events(correction_key_hash, event_id DESC)"
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
            connection.execute(
                "UPDATE xinchao_state SET pipes_updated_at="
                "COALESCE(pipes_updated_at, last_event_at, updated_at) WHERE state_id=1"
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
    def _state_pipe_anchor(state, fallback=None) -> datetime:
        """Return when the stored pipe snapshot was actually materialized."""
        raw = None
        if state is not None:
            try:
                raw = state["pipes_updated_at"]
            except (KeyError, IndexError):
                raw = None
            raw = raw or state["last_event_at"] or state["last_presence_at"]
        return parse_timestamp(raw or fallback or beijing_now())

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
    ) -> dict:
        def sanitize(value, depth: int = 0):
            if depth > 3:
                return None
            if isinstance(value, bool) or value is None:
                return value
            if isinstance(value, (int, float)):
                return value
            if isinstance(value, str):
                return value[:500]
            if isinstance(value, dict):
                return {
                    str(key)[:80]: cleaned
                    for key, item in list(value.items())[:80]
                    if (cleaned := sanitize(item, depth + 1)) is not None
                }
            if isinstance(value, (list, tuple)):
                return [
                    cleaned
                    for item in list(value)[:80]
                    if (cleaned := sanitize(item, depth + 1)) is not None
                ]
            return None

        safe_details = sanitize(details or {}) or {}
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
        correction_key: str = "",
    ) -> dict:
        timestamp = now_iso()
        fingerprint = self._fingerprint(content)
        external_event_hash = self._opaque_hash(external_event_id)
        correction_key_hash = self._opaque_hash(correction_key)
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
                  AND (?='' OR COALESCE(correction_key_hash, '')<>?)
                ORDER BY event_id DESC LIMIT 1
                """,
                (fingerprint, cutoff, correction_key_hash, correction_key_hash),
            ).fetchone()
            if duplicate:
                return {"status": "duplicate", "event_id": int(duplicate["event_id"])}
            previous = None
            if correction_key_hash:
                previous = connection.execute(
                    """
                    SELECT event_id FROM xinchao_events
                    WHERE correction_key_hash=?
                      AND status IN ('pending', 'processing', 'applied')
                    ORDER BY event_id DESC LIMIT 1
                    """,
                    (correction_key_hash,),
                ).fetchone()
            cursor = connection.execute(
                """
                INSERT INTO xinchao_events (
                    created_at, source_tool, source_ref, fingerprint, content,
                    prompt_hash, external_event_hash, correction_key_hash,
                    supersedes_event_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    timestamp,
                    str(source_tool)[:80],
                    str(source_ref or "")[:160],
                    fingerprint,
                    str(content),
                    self.evaluator.prompt_hash,
                    external_event_hash or None,
                    correction_key_hash or None,
                    int(previous["event_id"]) if previous else None,
                ),
            )
            return {
                "status": "pending",
                "event_id": int(cursor.lastrowid),
                "supersedes_event_id": int(previous["event_id"]) if previous else None,
            }

    def _rollback_superseded_sync(self, event_id: int) -> dict:
        """Undo a corrected write's derived state without changing source history."""
        moment = beijing_now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            current = connection.execute(
                "SELECT supersedes_event_id FROM xinchao_events WHERE event_id=?",
                (int(event_id),),
            ).fetchone()
            previous_id = int(current["supersedes_event_id"] or 0) if current else 0
            if previous_id <= 0:
                return {"status": "none", "supersedes_event_id": None}
            previous = connection.execute(
                "SELECT * FROM xinchao_events WHERE event_id=?", (previous_id,)
            ).fetchone()
            if not previous or str(previous["status"]) == "superseded":
                return {"status": "unchanged", "supersedes_event_id": previous_id}

            reversal = {}
            if str(previous["status"]) == "applied":
                try:
                    reversal = {
                        name: -float(value)
                        for name, value in json.loads(previous["deltas_json"] or "{}").items()
                        if name in PIPE_NAMES
                    }
                except (TypeError, ValueError, json.JSONDecodeError):
                    reversal = {}

            contribution_tags = [
                str(row["canonical_tag"])
                for row in connection.execute(
                    """
                    SELECT canonical_tag FROM xinchao_thought_contributions
                    WHERE source_event_id=? AND reverted_at IS NULL
                    """,
                    (previous_id,),
                ).fetchall()
            ]
            connection.execute(
                """
                UPDATE xinchao_thought_contributions SET reverted_at=?
                WHERE source_event_id=? AND reverted_at IS NULL
                """,
                (moment.isoformat(timespec="seconds"), previous_id),
            )
            for canonical_tag in contribution_tags:
                self._rebuild_thought_from_contributions_sync(
                    connection, canonical_tag, moment
                )
            if not contribution_tags:
                # Compatibility fallback for rows created before contribution
                # provenance existed.
                connection.execute(
                    """
                    UPDATE xinchao_thoughts SET occurrence_count=0, feed_count=0,
                        status='retired', retired_at=?, updated_at=?
                    WHERE source_event_id=? AND thought_kind<>'trace'
                    """,
                    (
                        moment.isoformat(timespec="seconds"),
                        moment.isoformat(timespec="seconds"),
                        previous_id,
                    ),
                )
            state = connection.execute(
                "SELECT * FROM xinchao_state WHERE state_id=1"
            ).fetchone()
            same_cycle = bool(
                state
                and state["cycle_open"]
                and int(previous["cycle_id"] or 0) == int(state["cycle_id"] or 0)
            )
            if same_cycle:
                thoughts = self._active_thoughts_sync(connection, moment)
                floors = self._combined_floors(thoughts)
                pipes = json.loads(state["pipes_json"])
                if state["last_event_at"]:
                    try:
                        pipe_anchor = self._state_pipe_anchor(state, state["last_event_at"])
                        if moment > pipe_anchor:
                            pipes = self.engine.evolve(
                                pipes,
                                pipe_anchor,
                                moment,
                                floors,
                                plateaus=self._active_plateaus_sync(connection, moment),
                                growth_origin=state["last_event_at"],
                            )
                    except (TypeError, ValueError, json.JSONDecodeError):
                        pass
                pipes = self.engine.apply_event(pipes, reversal, floors)
                connection.execute(
                    """
                    UPDATE xinchao_state SET pipes_json=?, pipes_updated_at=?, last_event_at=?,
                        last_presence_at=?, last_event_summary='旧写入影响已撤回',
                        last_event_tag='写入修正', cycle_origin='correction',
                        sleep_stage='awake', static_ready=0, static_started_at=NULL,
                        darkflow_stage=0, last_darkflow_at=NULL,
                        darkflow_retry_at=NULL, darkflow_failures=0,
                        updated_at=?, version=version+1
                    WHERE state_id=1
                    """,
                    (
                        json.dumps(pipes, ensure_ascii=False),
                        moment.isoformat(timespec="seconds"),
                        moment.isoformat(timespec="seconds"),
                        moment.isoformat(timespec="seconds"),
                        moment.isoformat(timespec="seconds"),
                    ),
                )
            connection.execute("DELETE FROM xinchao_darkflow WHERE slot_id=1")
            connection.execute(
                """
                UPDATE xinchao_events SET status='superseded',
                    superseded_by_event_id=?, processed_at=?
                WHERE event_id=?
                """,
                (int(event_id), moment.isoformat(timespec="seconds"), previous_id),
            )
            self._journal_sync(
                connection,
                "narrative_event_corrected",
                cycle_id=int(state["cycle_id"] or 0) if state else 0,
                source=str(previous["source_tool"] or "write"),
                details={
                    "superseded_event_id": previous_id,
                    "replacement_event_id": int(event_id),
                    "reversed_pipes": len(reversal) if same_cycle else 0,
                    "same_cycle": same_cycle,
                },
            )
        return {
            "status": "rolled_back",
            "supersedes_event_id": previous_id,
            "reversed_pipes": reversal if same_cycle else {},
        }

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
            "SELECT * FROM xinchao_thoughts "
            "WHERE expires_at > ? AND retired_at IS NULL AND status<>'retired' "
            "ORDER BY last_seen DESC",
            (moment.isoformat(timespec="seconds"),),
        ).fetchall()
        return [dict(row) for row in rows]

    @staticmethod
    def _active_plateaus_sync(
        connection: sqlite3.Connection, moment: datetime
    ) -> dict[str, str]:
        stamp = moment.isoformat(timespec="seconds")
        connection.execute("DELETE FROM xinchao_plateaus WHERE until_at<=?", (stamp,))
        rows = connection.execute(
            "SELECT pipe_name, until_at FROM xinchao_plateaus WHERE until_at>?",
            (stamp,),
        ).fetchall()
        return {str(row["pipe_name"]): str(row["until_at"]) for row in rows}

    def _set_plateaus_sync(
        self,
        connection: sqlite3.Connection,
        names: list[str],
        moment: datetime,
        source: str,
    ) -> None:
        until = (moment + timedelta(hours=self.satisfaction_plateau_hours)).isoformat(
            timespec="seconds"
        )
        stamp = moment.isoformat(timespec="seconds")
        for name in names:
            if name not in PIPE_NAMES:
                continue
            connection.execute(
                """
                INSERT INTO xinchao_plateaus (pipe_name, until_at, source, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(pipe_name) DO UPDATE SET
                    until_at=excluded.until_at, source=excluded.source,
                    updated_at=excluded.updated_at
                """,
                (name, until, str(source)[:80], stamp),
            )

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

    def _state_is_in_absence(self, state, moment: datetime) -> bool:
        """Return whether the open cycle has crossed the inactivity boundary."""
        if not state or not bool(state["cycle_open"]):
            return False
        if bool(state["static_ready"]):
            return True
        raw = state["last_presence_at"] or state["last_event_at"]
        if not raw:
            return False
        try:
            elapsed = (moment - parse_timestamp(raw)).total_seconds()
        except (TypeError, ValueError):
            return False
        return elapsed >= self.silence_to_absence_hours * 3600

    def _combined_floors(self, thoughts: list[dict]) -> dict[str, float]:
        floors = self._baseline_floors()
        for name, value in self._floors_from_thoughts(thoughts).items():
            floors[name] = max(floors.get(name, 0.0), float(value))
        return floors

    def _rebuild_thought_from_contributions_sync(
        self,
        connection: sqlite3.Connection,
        canonical_tag: str,
        moment: datetime,
    ) -> None:
        """Rebuild one merged thought after a source write is corrected."""
        rows = connection.execute(
            """
            SELECT * FROM xinchao_thought_contributions
            WHERE canonical_tag=? AND reverted_at IS NULL
            ORDER BY created_at, source_event_id
            """,
            (str(canonical_tag),),
        ).fetchall()
        stamp = moment.isoformat(timespec="seconds")
        if not rows:
            connection.execute(
                """
                UPDATE xinchao_thoughts SET occurrence_count=0, feed_count=0,
                    status='retired', floor_json='{}', retired_at=?, updated_at=?
                WHERE canonical_tag=? AND thought_kind<>'trace'
                """,
                (stamp, stamp, str(canonical_tag)),
            )
            return

        latest = rows[-1]
        count = len(rows)
        status = "obsession" if count >= self.obsession_repeats else "flash"
        retired_at = None
        if count > self.thought_feed_limit:
            status = "retired"
            retired_at = stamp
        lifetime = self.obsession_hours if status == "obsession" else self.flash_hours
        floor: dict[str, float] = {}
        if status == "obsession" and count <= self.thought_feed_limit:
            for contribution in rows:
                try:
                    deltas = json.loads(contribution["deltas_json"] or "{}")
                except (TypeError, ValueError, json.JSONDecodeError):
                    deltas = {}
                for name, raw_value in deltas.items():
                    if name not in PIPE_NAMES:
                        continue
                    try:
                        value = float(raw_value)
                    except (TypeError, ValueError):
                        continue
                    if value > 0:
                        limit = 0.05 if name in {"难过", "生气", "醋", "自省"} else 0.08
                        floor[name] = max(
                            floor.get(name, 0.0), round(min(limit, value * 0.15), 4)
                        )
        latest_at = parse_timestamp(latest["created_at"])
        expires_at = (latest_at + timedelta(hours=lifetime)).isoformat(
            timespec="seconds"
        )
        connection.execute(
            """
            UPDATE xinchao_thoughts SET event_tag=?, thought_kind=?,
                first_seen=?, last_seen=?, occurrence_count=?, status=?,
                floor_json=?, linkage_json=?, expires_at=?, thought_text=?,
                tone=?, intensity=?, reason=?, source_event_id=?, source_tool=?,
                source_ref=?, resolved_at=NULL, updated_at=?, feed_count=?,
                last_fed_at=?, retired_at=?
            WHERE canonical_tag=?
            """,
            (
                str(latest["event_tag"]),
                str(latest["thought_kind"]),
                str(rows[0]["created_at"]),
                str(latest["created_at"]),
                count,
                status,
                json.dumps(floor, ensure_ascii=False),
                str(latest["linkage_json"] or "{}"),
                expires_at,
                str(latest["thought_text"] or ""),
                str(latest["tone"] or "mixed"),
                float(max(float(row["intensity"] or 0.0) for row in rows)),
                str(latest["reason"] or ""),
                int(latest["source_event_id"]),
                str(latest["source_tool"] or ""),
                str(latest["source_ref"] or ""),
                stamp,
                count,
                str(latest["created_at"]),
                retired_at,
                str(canonical_tag),
            ),
        )

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
        thought_kind: str = "inner",
        linkage: dict | None = None,
    ) -> dict:
        row = connection.execute(
            "SELECT * FROM xinchao_thoughts WHERE canonical_tag=?", (canonical_tag,)
        ).fetchone()
        active = (
            row
            and not row["retired_at"]
            and str(row["status"]) != "retired"
            and parse_timestamp(row["expires_at"]) > moment
        )
        count = int(row["occurrence_count"]) + 1 if active else 1
        feed_count = int(row["feed_count"] or 0) + 1 if active else 1
        status = "obsession" if count >= self.obsession_repeats else "flash"
        retired_at = None
        if feed_count > self.thought_feed_limit:
            status = "retired"
            retired_at = moment.isoformat(timespec="seconds")
        lifetime = self.obsession_hours if status == "obsession" else self.flash_hours
        floor = {}
        if status == "obsession" and feed_count <= self.thought_feed_limit:
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
        safe_kind = "trace" if str(thought_kind).strip().lower() == "trace" else "inner"
        stored_thought_text = (
            str(thought_text)
            if safe_kind == "trace"
            else str(thought_text).strip()[:240]
        )
        connection.execute(
            """
            INSERT INTO xinchao_thoughts (
                canonical_tag, event_tag, thought_kind, first_seen, last_seen,
                occurrence_count, status, floor_json, linkage_json, expires_at,
                thought_text, tone, intensity, reason, source_event_id,
                source_tool, source_ref, privacy, resolved_at, updated_at,
                feed_count, last_fed_at, retired_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'inner_only', NULL, ?, ?, ?, ?)
            ON CONFLICT(canonical_tag) DO UPDATE SET
                event_tag=excluded.event_tag,
                thought_kind=excluded.thought_kind,
                first_seen=excluded.first_seen,
                last_seen=excluded.last_seen,
                occurrence_count=excluded.occurrence_count,
                status=excluded.status,
                floor_json=excluded.floor_json,
                linkage_json=excluded.linkage_json,
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
                updated_at=excluded.updated_at,
                feed_count=excluded.feed_count,
                last_fed_at=excluded.last_fed_at,
                retired_at=excluded.retired_at
            """,
            (
                canonical_tag,
                event_tag,
                safe_kind,
                first_seen,
                moment.isoformat(timespec="seconds"),
                count,
                status,
                json.dumps(floor, ensure_ascii=False),
                json.dumps(linkage or {}, ensure_ascii=False),
                expires_at,
                stored_thought_text,
                tone if tone in {"positive", "negative", "mixed"} else "mixed",
                max(0.0, min(1.0, float(intensity))),
                str(reason).strip()[:240],
                source_event_id,
                str(source_tool)[:80],
                str(source_ref)[:160],
                moment.isoformat(timespec="seconds"),
                feed_count,
                moment.isoformat(timespec="seconds"),
                retired_at,
            ),
        )
        if source_event_id is not None and safe_kind != "trace":
            connection.execute(
                """
                INSERT OR REPLACE INTO xinchao_thought_contributions (
                    source_event_id, canonical_tag, event_tag, thought_kind,
                    thought_text, tone, intensity, reason, source_tool,
                    source_ref, linkage_json, deltas_json, created_at, reverted_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL)
                """,
                (
                    int(source_event_id),
                    canonical_tag,
                    event_tag,
                    safe_kind,
                    stored_thought_text,
                    tone if tone in {"positive", "negative", "mixed"} else "mixed",
                    max(0.0, min(1.0, float(intensity))),
                    str(reason).strip()[:240],
                    str(source_tool)[:80],
                    str(source_ref)[:160],
                    json.dumps(linkage or {}, ensure_ascii=False),
                    json.dumps(deltas or {}, ensure_ascii=False),
                    moment.isoformat(timespec="seconds"),
                ),
            )
        saved = connection.execute(
            "SELECT * FROM xinchao_thoughts WHERE canonical_tag=?", (canonical_tag,)
        ).fetchone()
        return dict(saved) if saved else {}

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
                if bool(state["static_ready"]):
                    # A fresh write after silence starts a clean interaction
                    # cycle; the new event is applied to configured baselines.
                    cycle_id += 1
                    pipes = self._baseline_floors()
                else:
                    pipes = json.loads(state["pipes_json"])
                    previous = self._state_pipe_anchor(state, state["last_event_at"])
                    if moment > previous:
                        thoughts = self._active_thoughts_sync(connection, moment)
                        floors = self._combined_floors(thoughts)
                        pipes = self.engine.evolve(
                            pipes,
                            previous,
                            moment,
                            floors,
                            plateaus=self._active_plateaus_sync(connection, moment),
                            growth_origin=state["last_event_at"],
                        )
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
            composites = infer_composite_states(pipes)
            handoff_ready = bool(evaluation.get("handoff_ready", False))
            static_started_at = moment.isoformat(timespec="seconds") if handoff_ready else None
            connection.execute(
                "DELETE FROM xinchao_darkflow WHERE slot_id=1",
            )
            connection.execute(
                """
                UPDATE xinchao_state SET cycle_id=?, cycle_open=1,
                    last_event_at=?, pipes_updated_at=?, pipes_json=?, last_event_summary=?,
                    last_event_tag=?, last_presence_at=?, cycle_origin='event',
                    sleep_stage='awake',
                    sleep_started_at=NULL, deep_sleep_at=NULL,
                    darkflow_stage=0, last_darkflow_at=NULL,
                    darkflow_retry_at=NULL, darkflow_failures=0,
                    static_ready=?, static_started_at=?,
                    updated_at=?, version=version+1
                WHERE state_id=1
                """,
                (
                    cycle_id,
                    moment.isoformat(timespec="seconds"),
                    moment.isoformat(timespec="seconds"),
                    json.dumps(pipes, ensure_ascii=False),
                    evaluation["event"],
                    evaluation["event_tag"],
                    moment.isoformat(timespec="seconds"),
                    int(handoff_ready),
                    static_started_at,
                    processed_at,
                ),
            )
            connection.execute(
                """
                UPDATE xinchao_events SET content=NULL, event_summary=?, event_tag=?,
                    context_card=?, cycle_id=?, canonical_tag=?, severity=?,
                    deltas_json=?, signals_json=?, composites_json=?, narrative_complete=?,
                    quality_note=?, status='applied', error='', processed_at=?
                    , handoff_ready=?
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
                    json.dumps(evaluation.get("signals", []), ensure_ascii=False),
                    json.dumps(composites, ensure_ascii=False),
                    int(bool(evaluation.get("narrative_complete", True))),
                    evaluation.get("quality_note", ""),
                    processed_at,
                    int(handoff_ready),
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
                    "pipe_deltas": evaluation.get("pipes", {}),
                    "event_tag": evaluation.get("event_tag", ""),
                    "signal_count": len(evaluation.get("signals", [])),
                    "composite_states": composites,
                    "severity": float(evaluation.get("severity", 0.0)),
                    "narrative_complete": bool(
                        evaluation.get("narrative_complete", True)
                    ),
                    "handoff_ready": handoff_ready,
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
            "handoff_ready": handoff_ready,
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

    def _supersede_handoff_for_write_sync(self, event_id: int) -> dict:
        """Make every newly queued narrative write newer than pending absence output."""
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            event = connection.execute(
                "SELECT created_at, source_tool FROM xinchao_events WHERE event_id=?",
                (int(event_id),),
            ).fetchone()
            state = connection.execute(
                "SELECT * FROM xinchao_state WHERE state_id=1"
            ).fetchone()
            if not event or not state:
                return {"status": "missing"}

            moment = parse_timestamp(event["created_at"])
            previous_cycle_id = int(state["cycle_id"])
            cycle_id = previous_cycle_id
            was_in_absence = self._state_is_in_absence(state, moment)
            if state["cycle_open"]:
                if was_in_absence:
                    pipes = self._baseline_floors()
                    cycle_id += 1
                else:
                    pipes = json.loads(state["pipes_json"])
                    if state["last_event_at"]:
                        previous = self._state_pipe_anchor(state, state["last_event_at"])
                        if moment > previous:
                            thoughts = self._active_thoughts_sync(connection, moment)
                            pipes = self.engine.evolve(
                                pipes,
                                previous,
                                moment,
                                self._combined_floors(thoughts),
                                plateaus=self._active_plateaus_sync(connection, moment),
                                growth_origin=state["last_event_at"],
                            )
            else:
                cycle_id += 1
                pipes = self._baseline_floors()

            removed = connection.execute(
                "DELETE FROM xinchao_darkflow WHERE slot_id=1 AND status='pending'"
            ).rowcount
            connection.execute(
                """
                UPDATE xinchao_state SET cycle_id=?, cycle_open=1,
                    last_event_at=?, last_presence_at=?, pipes_updated_at=?, cycle_origin='event',
                    last_event_summary='', last_event_tag='', pipes_json=?,
                    sleep_stage='awake', sleep_started_at=NULL, deep_sleep_at=NULL,
                    darkflow_stage=0, last_darkflow_at=NULL,
                    darkflow_retry_at=NULL, darkflow_failures=0,
                    static_ready=0, static_started_at=NULL,
                    updated_at=?, version=version+1
                WHERE state_id=1
                """,
                (
                    cycle_id,
                    moment.isoformat(timespec="seconds"),
                    moment.isoformat(timespec="seconds"),
                    moment.isoformat(timespec="seconds"),
                    json.dumps(pipes, ensure_ascii=False),
                    now_iso(),
                ),
            )
            self._journal_sync(
                connection,
                "new_write_superseded_handoff",
                cycle_id=cycle_id,
                source=str(event["source_tool"] or "write"),
                from_stage=str(state["sleep_stage"] or "awake"),
                to_stage="awake",
                details={
                    "discarded_darkflow": bool(removed),
                    "previous_cycle_id": previous_cycle_id,
                    "new_cycle_started": bool(cycle_id != previous_cycle_id),
                    "reset_silence_effects": was_in_absence,
                },
            )
        return {
            "status": "superseded",
            "cycle_id": cycle_id,
            "previous_cycle_id": previous_cycle_id,
            "new_cycle_started": bool(cycle_id != previous_cycle_id),
            "reset_silence_effects": was_in_absence,
            "discarded_darkflow": bool(removed),
        }

    async def record_event(
        self,
        content: str,
        source_tool: str,
        source_ref: str = "",
        external_event_id: str = "",
        correction_key: str = "",
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
            correction_key,
        )
        if queued["status"] == "duplicate":
            return queued
        async with self._process_lock:
            correction = await asyncio.to_thread(
                self._rollback_superseded_sync, queued["event_id"]
            )
            pending = await asyncio.to_thread(
                self._pending_ids_sync, queued["event_id"], 2
            )
            for event_id in pending:
                await self._process_event(event_id)
            # A successful mailbox/bucket write is the newest truth even when
            # emotion evaluation later fails or decides the wording is a repeat.
            superseded = await asyncio.to_thread(
                self._supersede_handoff_for_write_sync, queued["event_id"]
            )
            result = await self._process_event(queued["event_id"])
            result["superseded"] = superseded
            if correction.get("status") == "rolled_back":
                result["correction"] = correction
            return result

    @staticmethod
    def _safe_trace_deltas(deltas: dict | None) -> dict[str, float]:
        """Keep a private trace emotionally meaningful but bounded."""
        result: dict[str, float] = {}
        remaining = 0.8
        for name, raw_value in (deltas or {}).items():
            if name not in PIPE_NAMES or remaining <= 0:
                continue
            try:
                value = max(-0.4, min(0.4, float(raw_value)))
            except (TypeError, ValueError):
                continue
            value = max(-remaining, min(remaining, value))
            if value:
                result[name] = round(value, 4)
                remaining = round(remaining - abs(value), 4)
        return result

    def _record_thought_trace_sync(
        self,
        text: str,
        tag: str,
        tone: str,
        intensity: float,
        reason: str,
        deltas: dict,
        source_ref: str,
    ) -> dict:
        moment = beijing_now()
        raw_text = str(text or "")
        if not raw_text.strip():
            return {"status": "ignored", "reason": "念痕不能为空"}
        if len(raw_text) > 240:
            return {"status": "ignored", "reason": "念痕不能超过 240 个字符"}
        safe_deltas = self._safe_trace_deltas(deltas)
        trace_tag = self._canonical_tag(tag or raw_text[:40])
        # Every AI-written trace is a separate temporal record.  The semantic
        # event_tag still groups recurring themes, while canonical_tag remains
        # an immutable identity that later edits can target precisely.
        trace_identity = uuid.uuid4().hex[:12]
        canonical_tag = f"trace:{trace_tag[:48]}:{trace_identity}"[:80]
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            state = connection.execute(
                "SELECT * FROM xinchao_state WHERE state_id=1"
            ).fetchone()
            if not state:
                return {"status": "unavailable"}
            thoughts = self._active_thoughts_sync(connection, moment)
            floors = self._combined_floors(thoughts)
            if state["cycle_open"] and state["last_event_at"]:
                try:
                    pipes = self.engine.evolve(
                        json.loads(state["pipes_json"]),
                        self._state_pipe_anchor(state, state["last_event_at"]),
                        moment,
                        floors,
                        plateaus=self._active_plateaus_sync(connection, moment),
                        growth_origin=state["last_event_at"],
                    )
                except (TypeError, ValueError, json.JSONDecodeError):
                    pipes = json.loads(state["pipes_json"])
            else:
                pipes = self._baseline_floors()
            cycle_id = int(state["cycle_id"])
            if not state["cycle_open"]:
                cycle_id += 1
            connection.execute("DELETE FROM xinchao_darkflow WHERE slot_id=1")
            saved = self._update_thought_sync(
                connection,
                canonical_tag,
                str(tag or raw_text[:40] or "念痕").strip()[:120],
                safe_deltas,
                moment,
                thought_text=raw_text,
                tone=tone,
                intensity=intensity,
                reason=reason,
                source_tool="mcp:thought_trace",
                source_ref=source_ref,
                thought_kind="trace",
                linkage=safe_deltas,
            )
            thoughts = self._active_thoughts_sync(connection, moment)
            updated = self.engine.apply_event(
                pipes, safe_deltas, self._combined_floors(thoughts)
            )
            connection.execute(
                """
                UPDATE xinchao_state SET cycle_id=?, cycle_open=1,
                    last_event_at=?, last_presence_at=?, pipes_updated_at=?, pipes_json=?,
                    last_event_summary='私密念痕已写入', last_event_tag='念痕',
                    cycle_origin='trace', sleep_stage='awake',
                    sleep_started_at=NULL, deep_sleep_at=NULL,
                    darkflow_stage=0, last_darkflow_at=NULL,
                    darkflow_retry_at=NULL, darkflow_failures=0,
                    static_ready=0, static_started_at=NULL,
                    updated_at=?, version=version+1
                WHERE state_id=1
                """,
                (
                    cycle_id,
                    moment.isoformat(timespec="seconds"),
                    moment.isoformat(timespec="seconds"),
                    moment.isoformat(timespec="seconds"),
                    json.dumps(updated, ensure_ascii=False),
                    moment.isoformat(timespec="seconds"),
                ),
            )
            self._journal_sync(
                connection,
                "thought_trace_recorded",
                cycle_id=cycle_id,
                source="mcp:thought_trace",
                details={
                    "thought_kind": "trace",
                    "changed_pipes": len(safe_deltas),
                    "pipe_deltas": safe_deltas,
                    "event_summary": "一条念痕牵动了内在状态",
                    "private": True,
                },
            )
        item = dict(saved)
        item["linkage"] = safe_deltas
        item.pop("floor_json", None)
        item["private"] = True
        item["kind_label"] = "念痕"
        item["read_only_from_manager"] = True
        return {
            "status": "recorded",
            "thought": item,
            "deltas": safe_deltas,
            "pipes": updated,
            "cycle_id": cycle_id,
            "privacy": "inner_only",
            "web_mutation": False,
        }

    def _update_thought_trace_sync(
        self,
        canonical_tag: str,
        text: str,
        tag: str,
        tone: str,
        intensity: float,
        reason: str,
        deltas: dict,
        source_ref: str,
    ) -> dict:
        moment = beijing_now()
        raw_text = str(text or "")
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM xinchao_thoughts WHERE canonical_tag=?",
                (str(canonical_tag),),
            ).fetchone()
            if not row:
                return {"status": "not_found", "reason": "没有找到这条念痕"}
            if str(row["thought_kind"] or "") != "trace":
                return {"status": "forbidden", "reason": "只能修改念痕，不能修改普通心念"}
            try:
                old_linkage = json.loads(row["linkage_json"] or "{}")
            except (TypeError, ValueError, json.JSONDecodeError):
                old_linkage = {}
            old_linkage = self._safe_trace_deltas(old_linkage)
            new_linkage = self._safe_trace_deltas(deltas)
            state = connection.execute(
                "SELECT * FROM xinchao_state WHERE state_id=1"
            ).fetchone()
            if not state:
                return {"status": "unavailable"}
            thoughts = self._active_thoughts_sync(connection, moment)
            floors = self._combined_floors(thoughts)
            if state["cycle_open"] and state["last_event_at"]:
                try:
                    pipes = self.engine.evolve(
                        json.loads(state["pipes_json"]),
                        self._state_pipe_anchor(state, state["last_event_at"]),
                        moment,
                        floors,
                        plateaus=self._active_plateaus_sync(connection, moment),
                        growth_origin=state["last_event_at"],
                    )
                except (TypeError, ValueError, json.JSONDecodeError):
                    pipes = json.loads(state["pipes_json"])
            else:
                pipes = self._baseline_floors()
            correction = {
                name: round(float(new_linkage.get(name, 0.0)) - float(old_linkage.get(name, 0.0)), 4)
                for name in PIPE_NAMES
                if float(new_linkage.get(name, 0.0)) != float(old_linkage.get(name, 0.0))
            }
            updated = self.engine.apply_event(pipes, correction, floors)
            cycle_id = int(state["cycle_id"])
            if not state["cycle_open"]:
                cycle_id += 1
            connection.execute(
                """
                INSERT INTO xinchao_trace_revisions (
                    canonical_tag, revised_at, old_text, new_text,
                    old_linkage_json, new_linkage_json, source_tool, source_ref
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    str(canonical_tag),
                    moment.isoformat(timespec="seconds"),
                    str(row["thought_text"] or ""),
                    raw_text,
                    json.dumps(old_linkage, ensure_ascii=False),
                    json.dumps(new_linkage, ensure_ascii=False),
                    "mcp:thought_trace_update",
                    str(source_ref)[:160],
                ),
            )
            connection.execute(
                """
                UPDATE xinchao_thoughts SET
                    event_tag=?, thought_text=?, tone=?, intensity=?, reason=?,
                    linkage_json=?, source_tool=?, source_ref=?, updated_at=?,
                    resolved_at=NULL, status=CASE WHEN status='resolved' THEN 'flash' ELSE status END
                WHERE canonical_tag=? AND thought_kind='trace'
                """,
                (
                    str(tag or row["event_tag"] or "念痕").strip()[:120],
                    raw_text,
                    tone if tone in {"positive", "negative", "mixed"} else str(row["tone"] or "mixed"),
                    max(0.0, min(1.0, float(intensity))),
                    str(reason).strip()[:240],
                    json.dumps(new_linkage, ensure_ascii=False),
                    "mcp:thought_trace_update",
                    str(source_ref)[:160],
                    moment.isoformat(timespec="seconds"),
                    str(canonical_tag),
                ),
            )
            connection.execute("DELETE FROM xinchao_darkflow WHERE slot_id=1")
            connection.execute(
                """
                UPDATE xinchao_state SET cycle_id=?, cycle_open=1,
                    last_event_at=?, last_presence_at=?, pipes_updated_at=?, pipes_json=?,
                    last_event_summary='私密念痕已修改', last_event_tag='念痕',
                    cycle_origin='trace', sleep_stage='awake',
                    sleep_started_at=NULL, deep_sleep_at=NULL,
                    darkflow_stage=0, last_darkflow_at=NULL,
                    darkflow_retry_at=NULL, darkflow_failures=0,
                    static_ready=0, static_started_at=NULL,
                    updated_at=?, version=version+1
                WHERE state_id=1
                """,
                (
                    cycle_id,
                    moment.isoformat(timespec="seconds"),
                    moment.isoformat(timespec="seconds"),
                    moment.isoformat(timespec="seconds"),
                    json.dumps(updated, ensure_ascii=False),
                    moment.isoformat(timespec="seconds"),
                ),
            )
            self._journal_sync(
                connection,
                "thought_trace_updated",
                cycle_id=cycle_id,
                source="mcp:thought_trace_update",
                details={
                    "private": True,
                    "text_changed": str(row["thought_text"] or "") != raw_text,
                    "changed_pipes": len(correction),
                },
            )
            saved = connection.execute(
                "SELECT * FROM xinchao_thoughts WHERE canonical_tag=?",
                (str(canonical_tag),),
            ).fetchone()
        item = dict(saved) if saved else {}
        item.pop("floor_json", None)
        item["linkage"] = new_linkage
        item["private"] = True
        item["kind_label"] = "念痕"
        item["read_only_from_manager"] = True
        return {
            "status": "updated",
            "thought": item,
            "deltas": new_linkage,
            "correction": correction,
            "pipes": updated,
            "cycle_id": cycle_id,
            "privacy": "inner_only",
            "web_mutation": False,
        }

    async def record_thought_trace(
        self,
        text: str,
        *,
        tag: str = "",
        tone: str = "mixed",
        intensity: float = 0.3,
        reason: str = "",
        deltas: dict | None = None,
        source_ref: str = "",
    ) -> dict:
        """Record a current-window private thought without creating a memory event."""
        if not self.enabled:
            return {"status": "disabled"}
        raw_text = str(text or "")
        if not raw_text.strip():
            return {"status": "ignored", "reason": "念痕不能为空"}
        if len(raw_text) > 240:
            return {"status": "ignored", "reason": "念痕不能超过 240 个字符"}
        # The conversational AI supplies the exact trace. DeepSeek/evaluator,
        # if used below, may return hormone linkage only; it never supplies
        # replacement text or any other content for this record.
        safe_deltas = self._safe_trace_deltas(deltas)
        evaluation_pending = False
        if not safe_deltas:
            evaluator = getattr(self.evaluator, "evaluate_trace_effect", None)
            if callable(evaluator):
                try:
                    judged = await evaluator(raw_text)
                    safe_deltas = self._safe_trace_deltas(judged.get("pipes"))
                except Exception as error:
                    logger.warning("Thought trace hormone linkage pending: %s", error)
                    evaluation_pending = True
        result = await asyncio.to_thread(
            self._record_thought_trace_sync,
            raw_text,
            tag,
            tone,
            intensity,
            reason,
            safe_deltas,
            source_ref,
        )
        if evaluation_pending:
            result["linkage_pending"] = True
        return result

    async def update_thought_trace(
        self,
        canonical_tag: str,
        text: str,
        *,
        tag: str = "",
        tone: str = "mixed",
        intensity: float = 0.3,
        reason: str = "",
        deltas: dict | None = None,
        source_ref: str = "",
    ) -> dict:
        """Allow only the current AI tool caller to revise an existing trace."""
        if not self.enabled:
            return {"status": "disabled"}
        raw_text = str(text or "")
        if not raw_text.strip():
            return {"status": "ignored", "reason": "念痕不能为空"}
        if len(raw_text) > 240:
            return {"status": "ignored", "reason": "念痕不能超过 240 个字符"}
        safe_deltas = self._safe_trace_deltas(deltas)
        evaluation_pending = False
        if not safe_deltas:
            evaluator = getattr(self.evaluator, "evaluate_trace_effect", None)
            if callable(evaluator):
                try:
                    judged = await evaluator(raw_text)
                    safe_deltas = self._safe_trace_deltas(judged.get("pipes"))
                except Exception as error:
                    logger.warning("Thought trace update linkage pending: %s", error)
                    evaluation_pending = True
            else:
                evaluation_pending = True
        if evaluation_pending:
            with self._connect() as connection:
                row = connection.execute(
                    "SELECT linkage_json FROM xinchao_thoughts WHERE canonical_tag=? AND thought_kind='trace'",
                    (str(canonical_tag),),
                ).fetchone()
            if row:
                try:
                    safe_deltas = self._safe_trace_deltas(json.loads(row["linkage_json"] or "{}"))
                except (TypeError, ValueError, json.JSONDecodeError):
                    safe_deltas = {}
        result = await asyncio.to_thread(
            self._update_thought_trace_sync,
            canonical_tag,
            raw_text,
            tag,
            tone,
            intensity,
            reason,
            safe_deltas,
            source_ref,
        )
        if evaluation_pending:
            result["linkage_pending"] = True
        return result

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
                   event_summary, event_tag, context_card,
                   deltas_json, signals_json, composites_json
            FROM (
                SELECT event_id, created_at, source_tool, source_ref,
                       event_summary, event_tag, context_card,
                       deltas_json, signals_json, composites_json
                FROM xinchao_events
                WHERE cycle_id=? AND status='applied'
                ORDER BY event_id DESC LIMIT ?
            )
            ORDER BY event_id ASC
            """,
            (int(cycle_id), max(1, min(20, int(limit)))),
        ).fetchall()
        contexts = []
        for row in rows:
            item = dict(row)
            for source_key, target_key in (
                ("deltas_json", "pipe_deltas"),
                ("signals_json", "signals"),
                ("composites_json", "composite_states"),
            ):
                try:
                    default = "{}" if target_key == "pipe_deltas" else "[]"
                    item[target_key] = json.loads(item.pop(source_key, default) or default)
                except (TypeError, ValueError, json.JSONDecodeError):
                    item[target_key] = {} if target_key == "pipe_deltas" else []
            contexts.append(item)
        return contexts

    def _recent_linkages_sync(self, limit: int = 30) -> list[dict]:
        """Return human-facing write -> state links from immutable event rows."""
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT event_id, created_at, source_tool, source_ref,
                       event_summary, event_tag, context_card, severity,
                       deltas_json, signals_json, composites_json, processed_at
                FROM xinchao_events
                WHERE status='applied' AND deltas_json NOT IN ('', '{}')
                  AND source_tool IN (
                    'mailbox', 'hold', 'grow', 'trace_append',
                    'manager_memory', 'manager_append', 'manager_create', 'memory'
                  )
                ORDER BY event_id DESC LIMIT ?
                """,
                (max(1, min(200, int(limit))),),
            ).fetchall()
        source_labels = {
            "mailbox": "信箱", "hold": "记忆写入", "grow": "记忆归档",
            "thought_trace": "念痕", "thought_trace_update": "念痕修改",
            "feedback": "表达回响", "behavior_feedback": "表达回响",
            "timeline": "事实变化", "tasks": "未竟",
        }
        result = []
        for row in rows:
            item = dict(row)
            for source_key, target_key, default in (
                ("deltas_json", "pipe_deltas", {}),
                ("signals_json", "signals", []),
                ("composites_json", "composite_states", []),
            ):
                try:
                    item[target_key] = json.loads(item.pop(source_key, "") or json.dumps(default))
                except (TypeError, ValueError, json.JSONDecodeError):
                    item[target_key] = default
            evidence = next(
                (str(signal.get("evidence") or "").strip() for signal in item["signals"]
                 if str(signal.get("evidence") or "").strip()),
                "",
            )
            item["evidence"] = evidence or str(item.get("context_card") or "").strip()
            raw_source = str(item.get("source_tool") or "").strip()
            item["source_label"] = source_labels.get(raw_source, "一次写入")
            item["summary"] = str(
                item.get("event_summary") or item.get("context_card") or item.get("event_tag") or ""
            ).strip()
            item["affected_pipes"] = []
            for name, raw_value in item["pipe_deltas"].items():
                try:
                    value = round(float(raw_value), 4)
                except (TypeError, ValueError):
                    continue
                item["affected_pipes"].append({"name": name, "delta": value})
            result.append(item)
        return result

    async def recent_linkages(self, limit: int = 30) -> list[dict]:
        if not self.enabled:
            return []
        return await asyncio.to_thread(self._recent_linkages_sync, limit)

    def _cycle_stage_hours(self, cycle_origin: str = "") -> list[float]:
        # Presence-only sessions do not create a narrative cycle. For a real
        # event cycle, the inactivity boundary opens absence; the configured
        # first stage is measured from that boundary, not from the last write.
        # With the normal [1, 2, 4, ...] schedule this means 30 minutes of
        # silence, then another hour before the first darkflow is generated.
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
                last_event_raw = state["last_event_at"] or state["last_presence_at"]
                last_activity_raw = state["last_presence_at"] or last_event_raw
                last_event = parse_timestamp(last_event_raw)
                pipe_anchor = self._state_pipe_anchor(state, last_event_raw)
                last_activity = parse_timestamp(last_activity_raw)
                inactivity_seconds = max(
                    0, int((moment - last_activity).total_seconds())
                )
                inactivity_boundary = int(self.silence_to_absence_hours * 3600)
                # A zero-hour first stage is an explicit test/configuration
                # override. Normal production cycles always wait for the
                # inactivity boundary, even if the evaluator says handoff_ready.
                immediate_stage = bool(
                    state["static_ready"]
                    and self.darkflow_stage_hours
                    and self.darkflow_stage_hours[0] <= 0
                )
                static_ready = inactivity_seconds >= inactivity_boundary or immediate_stage
                absence_started = (
                    last_activity
                    if immediate_stage and inactivity_seconds < inactivity_boundary
                    else last_activity + timedelta(seconds=inactivity_boundary)
                )
                pipes = json.loads(state["pipes_json"])
                active_end = min(moment, absence_started) if static_ready else moment
                # Stored timestamps intentionally use second precision.  Do not
                # manufacture a 0.000001 change from the sub-second remainder
                # of an immediate status read after a baseline reset.
                if (active_end - pipe_anchor).total_seconds() >= 1.0:
                    pipes = self.engine.evolve(
                        pipes,
                        pipe_anchor,
                        active_end,
                        floors,
                        plateaus=self._active_plateaus_sync(connection, moment),
                        growth_origin=last_event,
                    )
                absence_anchor = max(pipe_anchor, absence_started)
                if static_ready and moment > absence_anchor:
                    pipes = self.engine.evolve_absence(
                        pipes,
                        absence_anchor,
                        moment,
                        floors,
                        plateaus=self._active_plateaus_sync(connection, moment),
                        drowsy_after_hours=self.drowsy_after_hours,
                        sleep_after_hours=self.sleep_after_hours,
                        phase_origin=absence_started,
                    )
                elapsed = (
                    max(0, int((moment - absence_started).total_seconds()))
                    if static_ready
                    else 0
                )
                cycle_origin = str(state["cycle_origin"] or "event")
                interaction_phase = "absence" if static_ready else "active"
                dominant, dominant_value = self.engine.dominant(pipes)
                return {
                    "available": True,
                    "repeated": False,
                    "cycle_id": int(state["cycle_id"]),
                    "version": int(state["version"]),
                    "last_event_at": state["last_event_at"],
                    "last_presence_at": state["last_presence_at"],
                    "absence_started_at": (
                        absence_started.isoformat(timespec="seconds")
                        if static_ready
                        else None
                    ),
                    "static_ready": static_ready,
                    "static_started_at": state["static_started_at"],
                    "as_of": moment.isoformat(timespec="seconds"),
                    "elapsed_seconds": elapsed,
                    "since_event_seconds": max(
                        0, int((moment - last_event).total_seconds())
                    ),
                    "inactivity_seconds": inactivity_seconds,
                    "pipes": pipes,
                    "dominant": dominant,
                    "dominant_value": dominant_value,
                    "event_summary": state["last_event_summary"],
                    "cycle_origin": cycle_origin,
                    "interaction_phase": interaction_phase,
                    "silence_nudge_due": (
                        not static_ready
                        and inactivity_seconds >= int(self.presence_nudge_after_hours * 3600)
                    ),
                    "silence_to_absence_seconds": int(
                        self.silence_to_absence_hours * 3600
                    ),
                    "cycle_open": True,
                    "event_contexts": self._cycle_contexts_sync(
                        connection, int(state["cycle_id"])
                    ),
                    "obsessions": obsessions,
                    "thoughts": thoughts,
                    "sleep_stage": self._sleep_stage(elapsed) if static_ready else "awake",
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
                "cycle_open": False,
                "last_event_at": None,
                "last_presence_at": None,
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
                UPDATE xinchao_state SET pipes_json=?, pipes_updated_at=?, sleep_stage=?, sleep_started_at=?,
                    deep_sleep_at=?, darkflow_stage=?, last_darkflow_at=?,
                    darkflow_retry_at=NULL, darkflow_failures=0,
                    updated_at=?, version=version+1
                WHERE state_id=1
                """,
                (
                    json.dumps(updated_pipes, ensure_ascii=False),
                    moment.isoformat(timespec="seconds"),
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
            if not preview.get("static_ready"):
                return {"status": "waiting", "phase": "active", "stage_index": 0}
            target_stage = self._target_stage(
                preview.get("elapsed_seconds", 0),
                preview.get("cycle_origin", ""),
            )
            current_stage = int(preview.get("darkflow_stage", 0))
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
                    unresolved_tasks=[],
                )
                if isinstance(generated, dict):
                    generated_text = generated.get("text", "")
                    aftereffect = {}
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
            positive_names = sorted(name for name, value in safe_deltas.items() if value > 0)
            reflux_tag = self._canonical_tag(
                "表达后的内在回响:" + (",".join(positive_names) or "表达")
            )
            thought = self._update_thought_sync(
                connection,
                reflux_tag,
                "表达后的内在回响",
                safe_deltas,
                moment,
                thought_text=str(content).strip()[:240],
                tone="mixed",
                intensity=min(1.0, sum(abs(value) for value in safe_deltas.values()) * 4),
                reason="已经说出口的内容先成为一条心念；重复出现后才影响驱力。",
                source_tool="bark_output",
                source_ref=str(cycle_id),
            )
            occurrences = int(thought.get("occurrence_count", 1))
            if thought.get("status") == "retired":
                self._journal_sync(
                    connection,
                    "behavior_feedback_deferred",
                    cycle_id=int(cycle_id),
                    source="bark",
                    event_hash=self._opaque_hash(content),
                    details={"thought": reflux_tag, "occurrences": int(thought.get("occurrence_count", 1))},
                )
                return {"status": "deferred", "deltas": {}, "thought": reflux_tag}
            # The first successful outward expression feeds back immediately;
            # repeated expressions remain smaller and bounded.
            scale = 1.0 if occurrences < 2 else 0.4
            reflux_deltas = {
                name: round(value * scale, 4) for name, value in safe_deltas.items()
            }
            thoughts = self._active_thoughts_sync(connection, moment)
            floors = self._combined_floors(thoughts)
            stored = json.loads(state["pipes_json"])
            anchor = self._state_pipe_anchor(state, state["last_event_at"])
            if moment > anchor:
                stored = self.engine.evolve(
                    stored,
                    anchor,
                    moment,
                    floors,
                    plateaus=self._active_plateaus_sync(connection, moment),
                    growth_origin=state["last_event_at"],
                )
            updated = self.engine.apply_event(
                stored,
                reflux_deltas,
                floors,
            )
            connection.execute(
                """
                UPDATE xinchao_state SET pipes_json=?, pipes_updated_at=?, updated_at=?, version=version+1
                WHERE state_id=1
                """,
                (
                    json.dumps(updated, ensure_ascii=False),
                    moment.isoformat(timespec="seconds"),
                    now_iso(),
                ),
            )
            self._journal_sync(
                connection,
                "behavior_feedback_applied",
                cycle_id=int(cycle_id),
                source="bark",
                event_hash=self._opaque_hash(content),
                details={
                    "changed_pipes": len(reflux_deltas),
                    "pipe_deltas": reflux_deltas,
                    "event_summary": "表达后的感受发生了轻微回响",
                    "positive_total": round(sum(v for v in reflux_deltas.values() if v > 0), 4),
                    "negative_total": round(sum(v for v in reflux_deltas.values() if v < 0), 4),
                },
            )
        return {"status": "applied", "deltas": reflux_deltas, "thought": reflux_tag}

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
        """Legacy delivery recorder kept read-only for compatibility."""
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
            self._journal_sync(
                connection,
                "boot_snapshot_recorded",
                cycle_id=int(preview["cycle_id"]),
                from_stage=str(preview.get("sleep_stage", "")),
                to_stage=str(preview.get("sleep_stage", "")),
                elapsed_seconds=int(preview.get("elapsed_seconds", 0)),
                details={
                    "had_darkflow": bool(preview.get("darkflow")),
                    "read_only": True,
                },
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
            stamp = now_iso()
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
                (stamp, int(cycle_id)),
            )
            if cursor.rowcount > 0:
                baseline = self._baseline_floors()
                connection.execute(
                    """
                    UPDATE xinchao_state SET cycle_open=0,
                        last_event_at=NULL, last_presence_at=NULL,
                        last_event_summary='', last_event_tag='',
                        pipes_json=?, pipes_updated_at=?, cycle_origin='delivery',
                        sleep_stage='awake', sleep_started_at=NULL,
                        deep_sleep_at=NULL, static_ready=0,
                        static_started_at=NULL, darkflow_stage=0,
                        last_darkflow_at=?, darkflow_retry_at=NULL,
                        darkflow_failures=0, updated_at=?, version=version+1
                    WHERE state_id=1 AND cycle_id=?
                    """,
                    (
                        json.dumps(baseline, ensure_ascii=False),
                        stamp,
                        stamp,
                        stamp,
                        int(cycle_id),
                    ),
                )
                self._journal_sync(
                    connection,
                    "darkflow_delivered",
                    cycle_id=int(cycle_id),
                    elapsed_seconds=int(row["elapsed_seconds"] if row else 0),
                    details={"stage_index": int(row["stage_index"] if row else 0)},
                )
                self._journal_sync(
                    connection,
                    "cycle_closed_after_delivery",
                    cycle_id=int(cycle_id),
                    source="pulse_boot",
                    details={"reset_to_baseline": True},
                )
        return cursor.rowcount > 0

    async def mark_darkflow_delivered(self, cycle_id: int) -> bool:
        return await asyncio.to_thread(
            self._mark_darkflow_delivered_sync, cycle_id
        )

    def _discard_darkflow_sync(self, cycle_id: int, reason: str) -> bool:
        with self._connect() as connection:
            cursor = connection.execute(
                "DELETE FROM xinchao_darkflow WHERE slot_id=1 AND cycle_id=?",
                (int(cycle_id),),
            )
            if cursor.rowcount:
                self._journal_sync(
                    connection,
                    "darkflow_discarded",
                    cycle_id=int(cycle_id),
                    source="system",
                    details={"reason": str(reason)[:80]},
                )
        return bool(cursor.rowcount)

    async def discard_darkflow(self, cycle_id: int, reason: str = "newer_write") -> bool:
        return await asyncio.to_thread(
            self._discard_darkflow_sync, cycle_id, reason
        )

    def _acknowledge_seen_sync(self, moment: datetime) -> dict:
        """Partly satisfy response-related drives without starting a timer."""
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
            was_in_absence = self._state_is_in_absence(state, moment)
            pipes = self._baseline_floors() if was_in_absence else json.loads(state["pipes_json"])
            if not was_in_absence and state["cycle_open"] and state["last_event_at"]:
                try:
                    pipes = self.engine.evolve(
                        pipes,
                        self._state_pipe_anchor(state, state["last_event_at"]),
                        moment,
                        floors,
                        plateaus=self._active_plateaus_sync(connection, moment),
                        growth_origin=state["last_event_at"],
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
            self._set_plateaus_sync(
                connection,
                list(retain_excess),
                moment,
                source="acknowledged_seen",
            )
            pending_darkflow = connection.execute(
                "SELECT status FROM xinchao_darkflow "
                "WHERE slot_id=1 AND cycle_id=?",
                (previous_cycle_id,),
            ).fetchone()
            darkflow_carried = bool(
                pending_darkflow and pending_darkflow["status"] == "pending"
            )
            # Any explicit action belongs to the active window and invalidates
            # every product created by the old silence period.
            connection.execute(
                "DELETE FROM xinchao_darkflow WHERE slot_id=1 AND cycle_id=?",
                (previous_cycle_id,),
            )
            darkflow_carried = False
            connection.execute(
                """
                UPDATE xinchao_state SET cycle_id=?, cycle_open=1,
                    last_event_at=?, last_presence_at=?, pipes_updated_at=?, cycle_origin='acknowledgement',
                    last_event_summary='', last_event_tag='', pipes_json=?,
                    sleep_stage='awake', sleep_started_at=NULL, deep_sleep_at=NULL,
                    darkflow_stage=0, last_darkflow_at=NULL,
                    darkflow_retry_at=NULL, darkflow_failures=0,
                    static_ready=0, static_started_at=NULL,
                    updated_at=?, version=version+1
                WHERE state_id=1
                """,
                (
                    next_cycle_id,
                    stamp,
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
                    "reset_silence_effects": was_in_absence,
                },
            )
        return {
            "status": "acknowledged",
            "previous_cycle_id": previous_cycle_id,
            "cycle_id": next_cycle_id,
            "active_started_at": stamp,
            "silence_started_at": None,
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
        """Deprecated compatibility hook; silence timers no longer exist."""
        return {
            "status": "disabled",
            "reason": "silence_timer_removed",
            "silence_started_at": None,
        }

    async def restart_silence_timer(self) -> dict:
        """Deprecated compatibility hook with no state changes."""
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
        interrupt_silence: bool,
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
            previous_cycle_id = int(state["cycle_id"] or 0)
            cycle_id = previous_cycle_id
            cycle_open = bool(state["cycle_open"])
            discarded_darkflow = 0
            was_in_absence = bool(
                interrupt_silence and self._state_is_in_absence(state, moment)
            )
            if interrupt_silence:
                # Any real AI action makes the old absence output obsolete.
                # Bump the version so a concurrently evaluating darkflow cannot
                # save itself again after this activity has already returned.
                discarded_darkflow = connection.execute(
                    "DELETE FROM xinchao_darkflow "
                    "WHERE slot_id=1 AND status='pending'"
                ).rowcount
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
            stamp = moment.isoformat(timespec="seconds")
            start_presence_cycle = bool(
                interrupt_silence and (was_in_absence or not cycle_open)
            )
            if start_presence_cycle:
                cycle_id += 1
                pipes = self._baseline_floors()
                connection.execute(
                    """
                    UPDATE xinchao_state SET cycle_id=?, cycle_open=1,
                        last_event_at=?, last_presence_at=?, pipes_updated_at=?,
                        pipes_json=?, cycle_origin='presence',
                        last_event_summary='', last_event_tag='',
                        sleep_stage='awake', sleep_started_at=NULL,
                        deep_sleep_at=NULL, darkflow_stage=0,
                        last_darkflow_at=NULL, darkflow_retry_at=NULL,
                        darkflow_failures=0, static_ready=0,
                        static_started_at=NULL, updated_at=?,
                        version=version+1
                    WHERE state_id=1
                    """,
                    (
                        cycle_id,
                        stamp,
                        stamp,
                        stamp,
                        json.dumps(pipes, ensure_ascii=False),
                        now_iso(),
                    ),
                )
            elif interrupt_silence and cycle_open:
                connection.execute(
                    """
                    UPDATE xinchao_state SET last_presence_at=?,
                        sleep_stage='awake', sleep_started_at=NULL,
                        deep_sleep_at=NULL, darkflow_stage=0,
                        last_darkflow_at=NULL, darkflow_retry_at=NULL,
                        darkflow_failures=0, static_ready=0,
                        static_started_at=NULL, updated_at=?,
                        version=version+1
                    WHERE state_id=1
                    """,
                    (stamp, now_iso()),
                )
            else:
                connection.execute(
                    "UPDATE xinchao_state SET last_presence_at=?, updated_at=? WHERE state_id=1",
                    (stamp, now_iso()),
                )
            self._journal_sync(
                connection,
                "activity_interrupted_silence" if interrupt_silence else "presence_observed",
                cycle_id=cycle_id,
                source=source,
                session_hash=session_hash,
                event_hash=event_hash,
                from_stage=previous_stage,
                to_stage="awake" if interrupt_silence and cycle_open else previous_stage,
                details={
                    "timer_started": False,
                    "timer_restarted": bool(interrupt_silence and cycle_open),
                    "discarded_darkflow": bool(discarded_darkflow),
                    "previous_cycle_id": previous_cycle_id,
                    "new_cycle_started": start_presence_cycle,
                    "reset_silence_effects": was_in_absence,
                },
            )
        return {
            "status": "observed",
            "woke": bool(interrupt_silence and cycle_open),
            "cycle_started": start_presence_cycle,
            "cycle_id": cycle_id,
            "previous_cycle_id": previous_cycle_id,
            "new_cycle_started": start_presence_cycle,
            "active_started_at": stamp if interrupt_silence else None,
            "timer_restarted": bool(interrupt_silence and cycle_open),
            "discarded_darkflow": bool(discarded_darkflow),
            "reset_silence_effects": was_in_absence,
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
        interrupt_silence: bool = False,
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
            bool(interrupt_silence),
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

    def _latest_boot_delivery_sync(self) -> dict | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT * FROM xinchao_boot_deliveries
                ORDER BY delivered_at DESC
                LIMIT 1
                """
            ).fetchone()
        return dict(row) if row else None

    async def latest_boot_delivery(self) -> dict | None:
        if not self.enabled:
            return None
        return await asyncio.to_thread(self._latest_boot_delivery_sync)

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
            await asyncio.to_thread(self._close_cycle_after_boot_sync)

    def _close_cycle_after_boot_sync(self) -> bool:
        """Close the delivered window even when it had no darkflow text."""
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            state = connection.execute(
                "SELECT cycle_id, cycle_open FROM xinchao_state WHERE state_id=1"
            ).fetchone()
            if not state or not state["cycle_open"]:
                return False
            stamp = now_iso()
            connection.execute(
                """
                UPDATE xinchao_state SET cycle_open=0,
                    last_event_at=NULL, last_presence_at=NULL,
                    last_event_summary='', last_event_tag='',
                    pipes_json=?, pipes_updated_at=?, cycle_origin='delivery',
                    sleep_stage='awake', sleep_started_at=NULL,
                    deep_sleep_at=NULL, static_ready=0,
                    static_started_at=NULL, darkflow_stage=0,
                    last_darkflow_at=COALESCE(last_darkflow_at, ?),
                    darkflow_retry_at=NULL, darkflow_failures=0,
                    updated_at=?, version=version+1
                WHERE state_id=1
                """,
                (
                    json.dumps(self._baseline_floors(), ensure_ascii=False),
                    stamp,
                    stamp,
                    stamp,
                ),
            )
            self._journal_sync(
                connection,
                "cycle_closed_after_boot",
                cycle_id=int(state["cycle_id"]),
                source="pulse_boot",
                details={"reset_to_baseline": True},
            )
            return True

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

    def _personality_preview_sync(self, days: int) -> dict:
        """Observe slow recurring patterns as a separate, behavior-facing profile."""
        safe_days = max(7, min(365, int(days)))
        cutoff = (beijing_now() - timedelta(days=safe_days)).isoformat(
            timespec="seconds"
        )
        with self._connect() as connection:
            events = connection.execute(
                """
                SELECT event_tag, COUNT(*) AS occurrences,
                       AVG(severity) AS average_severity,
                       MAX(created_at) AS last_seen
                FROM xinchao_events
                WHERE status='applied' AND created_at>=?
                  AND event_tag NOT IN ('', '念痕')
                  AND source_tool NOT IN ('bark_output', 'heartbeat', 'initialize',
                                          'xinchao_status', 'inner_state')
                GROUP BY event_tag
                HAVING COUNT(*)>=2
                ORDER BY occurrences DESC, average_severity DESC
                LIMIT 12
                """,
                (cutoff,),
            ).fetchall()
            thoughts = connection.execute(
                """
                SELECT event_tag,
                       SUM(CASE WHEN thought_kind='trace' THEN 1 ELSE occurrence_count END)
                           AS occurrence_count,
                       MAX(status) AS status,
                       MAX(last_seen) AS last_seen,
                       SUM(CASE WHEN thought_kind='trace' THEN 1 ELSE 0 END)
                           AS trace_count
                FROM xinchao_thoughts
                WHERE first_seen>=? AND status NOT IN ('retired', 'resolved')
                  AND event_tag NOT IN ('', '念痕')
                  AND source_tool<>'bark_output'
                GROUP BY event_tag
                HAVING SUM(CASE WHEN thought_kind='trace' THEN 1 ELSE occurrence_count END)>=2
                ORDER BY occurrence_count DESC, last_seen DESC
                LIMIT 12
                """,
                (cutoff,),
            ).fetchall()
            recent_events = connection.execute(
                """
                SELECT event_id, created_at, source_tool, source_ref,
                       event_summary, event_tag, deltas_json
                FROM xinchao_events
                WHERE status='applied' AND created_at>=?
                  AND source_tool NOT IN ('bark_output', 'heartbeat', 'initialize',
                                          'xinchao_status', 'inner_state')
                ORDER BY created_at DESC, event_id DESC LIMIT 20
                """,
                (cutoff,),
            ).fetchall()
            recent_traces = connection.execute(
                """
                SELECT canonical_tag, first_seen, last_seen, event_tag,
                       thought_text, source_tool, source_ref, linkage_json
                FROM xinchao_thoughts
                WHERE thought_kind='trace' AND status NOT IN ('retired', 'resolved')
                  AND first_seen>=?
                  AND source_tool<>'bark_output'
                ORDER BY last_seen DESC LIMIT 20
                """,
                (cutoff,),
            ).fetchall()
        patterns = []
        tendencies = []

        def human_tendency_label(raw_label: str) -> str:
            """Turn recurring evidence into a character tendency, not a log label."""
            raw = str(raw_label or "").strip()
            lowered = raw.casefold()
            if not raw or any(
                token in lowered
                for token in (
                    "输出回流", "表达后的内在回响", "mcp:", "heartbeat",
                    "initialize", "xinchao_status", "inner_state", "状态发生变化",
                )
            ):
                return ""
            mappings = (
                (("想靠近", "靠近", "继续说", "没有说完"), "更愿意靠近"),
                (("想黏着", "依恋", "陪伴"), "更珍惜持续的陪伴"),
                (("想照顾", "让她开心", "关心", "照料"), "更细致地照顾"),
                (("被理解", "被确认", "得到回应", "求证"), "更愿意确认彼此的心意"),
                (("修复关系", "和好", "道歉"), "更主动修复关系"),
                (("复盘", "自省", "反思", "权衡"), "更习惯复盘后再行动"),
                (("责任", "承担", "完成"), "更愿意承担"),
                (("暂时独处", "回避", "压抑", "退开"), "更倾向先安静整理自己"),
                (("分享", "表达", "说出口"), "更愿意把感受说出来"),
            )
            for needles, label in mappings:
                if any(needle in raw for needle in needles):
                    return label
            return raw[:40]

        def recency_strength(last_seen: str, evidence: int, severity: float) -> float:
            try:
                age_days = max(
                    0.0,
                    (beijing_now() - parse_timestamp(last_seen)).total_seconds() / 86400.0,
                )
            except (TypeError, ValueError):
                age_days = float(safe_days)
            evidence_score = min(1.0, 0.18 + 0.14 * max(0, evidence - 1))
            recency_score = max(0.2, 1.0 - age_days / max(7.0, safe_days * 1.25))
            severity_score = max(0.0, min(1.0, float(severity or 0.0)))
            return round(
                min(1.0, evidence_score * 0.45 + recency_score * 0.35 + severity_score * 0.20),
                3,
            )

        for row in events[:6]:
            raw_label = str(row["event_tag"] or "").strip()
            display_label = human_tendency_label(raw_label)
            if not display_label:
                continue
            item = {
                "pattern": display_label,
                "evidence_label": raw_label,
                "evidence_count": int(row["occurrences"]),
                "average_severity": round(float(row["average_severity"] or 0), 3),
                "last_seen": str(row["last_seen"] or ""),
            }
            patterns.append(item)
            tendencies.append(
                {
                    "tendency_id": f"event:{raw_label}",
                    "kind": "event_pattern",
                    "label": display_label,
                    "evidence_label": raw_label,
                    "evidence_count": item["evidence_count"],
                    "average_severity": item["average_severity"],
                    "last_seen": item["last_seen"],
                    "strength": recency_strength(
                        item["last_seen"], item["evidence_count"], item["average_severity"]
                    ),
                    "behavior_rule": "只在相似情境下作为软倾向参考，不是必须执行的命令。",
                }
            )
        for row in thoughts[:6]:
            thought_item = dict(row)
            raw_label = str(thought_item.get("event_tag") or "反复心念").strip()
            label = human_tendency_label(raw_label)
            if not label:
                continue
            count = int(thought_item.get("occurrence_count") or 0)
            last_seen = str(thought_item.get("last_seen") or "")
            tendencies.append(
                {
                    "tendency_id": f"thought:{raw_label}",
                    "kind": "private_thought_pattern",
                    "label": label,
                    "evidence_label": raw_label,
                    "evidence_count": count,
                    "average_severity": 0.0,
                    "last_seen": last_seen,
                    "strength": recency_strength(last_seen, count, 0.0),
                    "behavior_rule": "只影响相似情境下的表达方式和时机，不直接触发外部动作。",
                    "private_source": True,
                }
            )
        tendencies.sort(
            key=lambda item: (
                float(item.get("strength", 0.0)),
                int(item.get("evidence_count", 0)),
                str(item.get("last_seen", "")),
            ),
            reverse=True,
        )
        current = tendencies[0] if tendencies else {}
        evidence = []
        current_label = str(current.get("evidence_label") or current.get("label") or "")
        with self._connect() as connection:
            matching_traces = connection.execute(
                """
                SELECT canonical_tag, first_seen, last_seen, event_tag,
                       thought_text, reason, source_tool, source_ref, linkage_json
                FROM xinchao_thoughts
                WHERE event_tag=? AND first_seen>=? AND source_tool<>'bark_output'
                ORDER BY last_seen DESC LIMIT 12
                """,
                (current_label, cutoff),
            ).fetchall() if current_label else []
            matching_events = connection.execute(
                """
                SELECT event_id, created_at, source_tool, source_ref,
                       event_summary, event_tag, context_card, deltas_json
                FROM xinchao_events
                WHERE status='applied' AND event_tag=? AND created_at>=?
                  AND source_tool NOT IN ('bark_output', 'heartbeat', 'initialize',
                                          'xinchao_status', 'inner_state')
                ORDER BY created_at DESC, event_id DESC LIMIT 12
                """,
                (current_label, cutoff),
            ).fetchall() if current_label else []
        for row in matching_traces:
            item = dict(row)
            try:
                effects = json.loads(item.get("linkage_json") or "{}")
            except (TypeError, ValueError, json.JSONDecodeError):
                effects = {}
            evidence.append(
                {
                    "id": str(item.get("canonical_tag") or ""),
                    "created_at": str(item.get("last_seen") or item.get("first_seen") or ""),
                    "source": "念痕",
                    "title": str(item.get("event_tag") or "念痕"),
                    "summary": str(item.get("thought_text") or ""),
                    "reason": str(item.get("reason") or "这份当下感受反复出现"),
                    "effects": effects,
                }
            )
        for row in matching_events:
            item = dict(row)
            try:
                effects = json.loads(item.get("deltas_json") or "{}")
            except (TypeError, ValueError, json.JSONDecodeError):
                effects = {}
            evidence.append(
                {
                    "id": f"event:{item.get('event_id')}",
                    "created_at": str(item.get("created_at") or ""),
                    "source": {
                        "mailbox": "信箱", "hold": "记忆写入", "grow": "记忆归档",
                        "thought_trace": "念痕", "thought_trace_update": "念痕修改",
                    }.get(str(item.get("source_tool") or ""), "一次写入"),
                    "title": str(item.get("event_tag") or "一次写入"),
                    "summary": str(item.get("event_summary") or item.get("context_card") or ""),
                    "reason": "相似处境再次牵动了同一组感受与选择",
                    "effects": effects,
                }
            )
        evidence.sort(key=lambda item: str(item.get("created_at") or ""), reverse=True)
        evidence = evidence[:12]
        formation_reason = ""
        if current:
            formation_reason = (
                f"近 {safe_days} 天里，“{current.get('label', '')}”在相似处境中"
                f"反复出现 {int(current.get('evidence_count', 0) or 0)} 次；"
                "它会在相似情境中轻微影响表达方式和行动时机，但不会改写固定人格。"
            )
        return {
            "mode": "behavior_guidance_read_only",
            "days": safe_days,
            "patterns": patterns,
            # Keep the old key for clients that already read it.
            "suggestions": patterns,
            "recurring_thoughts": [dict(row) for row in thoughts],
            "tendencies": tendencies[:12],
            "tendency": {
                "name": current.get("label", ""),
                "label": current.get("label", ""),
                "score": current.get("strength", 0.0),
                "strength": current.get("strength", 0.0),
                "delta": 0.0,
                "kind": current.get("kind", ""),
                "description": formation_reason,
            } if current else {},
            "evidence": evidence,
            "evidence_count": int(current.get("evidence_count", 0) or 0),
            "formation_reason": formation_reason,
            "core_personality_unchanged": True,
            "rewrites_identity": False,
            "rewrites_memory": False,
        }

    async def personality_preview(self, days: int = 30) -> dict:
        if not self.enabled:
            return {
                "mode": "observation_only",
                "disabled": True,
                "patterns": [],
                "suggestions": [],
            }
        return await asyncio.to_thread(self._personality_preview_sync, days)

    async def disposition_preview(self, days: int = 30) -> dict:
        """Read the separate 性格轨迹 layer used as soft behavior guidance."""
        report = await self.personality_preview(days)
        return {
            "name": "性格轨迹",
            "mode": "behavior_guidance_read_only",
            "days": report.get("days", days),
            "patterns": list(report.get("patterns") or []),
            "suggestions": list(report.get("suggestions") or []),
            "recurring_thoughts": list(report.get("recurring_thoughts") or []),
            "tendencies": list(report.get("tendencies") or []),
            "tendency": dict(report.get("tendency") or {}),
            "evidence": list(report.get("evidence") or []),
            "evidence_count": int(report.get("evidence_count") or 0),
            "formation_reason": str(report.get("formation_reason") or ""),
            "core_personality_unchanged": True,
            "rewrites_identity": False,
            "rewrites_memory": False,
        }

    async def behavior_tendency_context(self, days: int = 30, limit: int = 6) -> dict:
        """Return bounded, non-command tendencies for behavior decisions."""
        report = await self.disposition_preview(days)
        tendencies = [
            item
            for item in report.get("tendencies", [])
            if float(item.get("strength", 0.0) or 0.0) >= 0.2
        ][: max(1, min(12, int(limit)))]
        return {
            "name": "性格轨迹",
            "mode": "soft_context_only",
            "tendencies": tendencies,
            "rules": [
                "只在当前事件与倾向相似时参考。",
                "倾向影响语气、时机和表达方式，不直接命令发送。",
                "固定人格边界、当前事件和安全规则优先。",
            ],
        }

    async def status(self) -> dict:
        if not self.enabled:
            return {"available": False, "disabled": True}
        moment = beijing_now()
        preview = await asyncio.to_thread(self._preview_sync, moment)
        preview["catalog"] = pipe_catalog()
        preview["composite_states"] = infer_composite_states(preview.get("pipes"))
        preview["expression_state"] = (
            preview["composite_states"][0]
            if preview["composite_states"]
            else {
                "name": preview.get("dominant", "平稳"),
                "score": round(float(preview.get("dominant_value", 0.0)), 4),
                "components": [],
            }
        )
        preview["rhythm"] = await asyncio.to_thread(
            self._rhythm_sync,
            moment,
            int(preview.get("elapsed_seconds", 0)),
        )
        darkflow = await asyncio.to_thread(self._darkflow_status_sync, False)
        timing = {
            "phase": preview.get("interaction_phase", "closed"),
            "last_activity_at": preview.get("last_presence_at") or preview.get("last_event_at"),
            "silence_to_absence_seconds": int(preview.get("silence_to_absence_seconds") or 0),
            "absence_due_at": None,
            "absence_started_at": preview.get("absence_started_at"),
            "next_darkflow_due_at": None,
            "generation_status": "generated" if darkflow else "waiting",
            "generated_at": (darkflow or {}).get("created_at"),
            "delivery_status": (darkflow or {}).get("status") or "not_generated",
            "delivered_at": (darkflow or {}).get("delivered_at"),
        }
        if preview.get("cycle_open") and timing["last_activity_at"]:
            try:
                absence_due = parse_timestamp(timing["last_activity_at"]) + timedelta(
                    seconds=timing["silence_to_absence_seconds"]
                )
                timing["absence_due_at"] = absence_due.isoformat(timespec="seconds")
                absence_start = parse_timestamp(preview.get("absence_started_at") or timing["absence_due_at"])
                timing["next_darkflow_due_at"] = self._next_stage_at(
                    absence_start,
                    int(preview.get("darkflow_stage") or 0),
                    str(preview.get("cycle_origin") or "event"),
                )
            except (TypeError, ValueError):
                pass
        if not preview.get("cycle_open"):
            timing["generation_status"] = "cycle_closed"
        preview["timing"] = timing
        return preview

    def _list_private_thoughts_sync(
        self, status: str = "active", limit: int = 100, kind: str = "all"
    ) -> list[dict]:
        moment = beijing_now()
        params: list = []
        clauses = ["t.thought_text<>''", "t.privacy='inner_only'"]
        safe_kind = str(kind or "all").strip().lower()
        if safe_kind in {"trace", "nianzhen", "念痕"}:
            clauses.append("t.thought_kind='trace'")
        elif safe_kind in {"inner", "thought", "心念"}:
            clauses.append("t.thought_kind<>'trace'")
        if status == "all":
            pass
        elif status == "flash":
            clauses.extend(["t.status='flash'", "t.resolved_at IS NULL", "t.expires_at>?"])
            params.append(moment.isoformat(timespec="seconds"))
        elif status == "obsession":
            clauses.extend(["t.status='obsession'", "t.resolved_at IS NULL", "t.expires_at>?"])
            params.append(moment.isoformat(timespec="seconds"))
        elif status == "resolved":
            clauses.append("t.resolved_at IS NOT NULL")
        elif status == "faded":
            clauses.extend(["t.resolved_at IS NULL", "t.expires_at<=?"])
            params.append(moment.isoformat(timespec="seconds"))
        else:
            clauses.extend(
                ["t.resolved_at IS NULL", "t.retired_at IS NULL", "t.status<>'retired'", "t.expires_at>?"]
            )
            params.append(moment.isoformat(timespec="seconds"))
        params.append(max(1, min(500, int(limit))))
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT t.*, e.event_summary AS source_summary, "
                "e.context_card AS source_context, e.event_tag AS source_event_tag, "
                "e.created_at AS source_created_at, e.deltas_json AS source_deltas_json "
                "FROM xinchao_thoughts t LEFT JOIN xinchao_events e "
                "ON e.event_id=t.source_event_id WHERE "
                + " AND ".join(clauses)
                + " ORDER BY t.intensity DESC, t.last_seen DESC LIMIT ?",
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
            for source_key, target_key in (
                ("linkage_json", "linkage"),
                ("source_deltas_json", "source_deltas"),
            ):
                try:
                    item[target_key] = json.loads(item.pop(source_key, "{}") or "{}")
                except (TypeError, ValueError, json.JSONDecodeError):
                    item[target_key] = {}
            item["private"] = True
            if item.get("thought_kind") == "trace":
                item["kind_label"] = "念痕"
            else:
                item["kind_label"] = {
                    "flash": "闪念", "obsession": "执念", "resolved": "已放下"
                }.get(str(item.get("status") or ""), "心念")
            item["source_label"] = {
                "mailbox": "信箱", "hold": "记忆写入", "grow": "记忆归档",
                "thought_trace": "念痕", "thought_trace_update": "念痕修改",
                "behavior_feedback": "表达回响", "feedback": "表达回响",
            }.get(str(item.get("source_tool") or ""), "一次写入")
            item["read_only_from_manager"] = item.get("thought_kind") == "trace"
            result.append(item)
        return result

    async def list_private_thoughts(
        self, status: str = "active", limit: int = 100, kind: str = "all"
    ) -> list[dict]:
        if not self.enabled:
            return []
        return await asyncio.to_thread(
            self._list_private_thoughts_sync, status, limit, kind
        )

    async def search_private_thoughts(
        self,
        query: str = "",
        *,
        kind: str = "all",
        date: str = "",
        limit: int = 30,
    ) -> list[dict]:
        """Read-only keyword + semantic search across private thought records."""
        if not self.enabled:
            return []
        clean_query = re.sub(r"\s+", " ", str(query or "")).strip()
        target_date = str(date or "").strip()
        items = await self.list_private_thoughts(status="all", limit=5000, kind=kind)
        if target_date:
            items = [
                item
                for item in items
                if target_date in {
                    str(item.get("first_seen") or "")[:10],
                    str(item.get("last_seen") or "")[:10],
                    str(item.get("updated_at") or "")[:10],
                }
            ]
        if not clean_query:
            items.sort(
                key=lambda item: str(
                    item.get("last_seen") or item.get("updated_at") or item.get("first_seen") or ""
                ),
                reverse=True,
            )
            return items[: max(1, min(500, int(limit)))]

        semantic_scores: dict[str, dict] = {}
        provider = self.thought_embedding_provider
        if provider is not None and getattr(provider, "enabled", False):
            synthetic = []
            for item in items:
                synthetic.append(
                    {
                        "id": f"thought:{item.get('canonical_tag', '')}",
                        "metadata": {
                            "name": item.get("thought_text", ""),
                            "domain": [item.get("kind_label", "心念")],
                            "tags": [item.get("event_tag", "")],
                        },
                        "content": "\n".join(
                            [
                                str(item.get("thought_text") or ""),
                                str(item.get("reason") or ""),
                                str(item.get("event_tag") or ""),
                            ]
                        ),
                    }
                )
            try:
                semantic_scores, _ = await provider.query_segment_matches(
                    clean_query, synthetic
                )
            except Exception as error:
                logger.warning("Private thought semantic search unavailable: %s", error)

        scored = []
        folded_query = clean_query.casefold()
        for item in items:
            corpus = " ".join(
                str(item.get(key) or "")
                for key in ("thought_text", "reason", "event_tag", "canonical_tag")
            )
            direct = folded_query in corpus.casefold()
            keyword_score = max(
                float(fuzz.WRatio(clean_query, str(item.get("thought_text") or ""))),
                float(fuzz.partial_ratio(clean_query, corpus)),
            )
            semantic = semantic_scores.get(
                f"thought:{item.get('canonical_tag', '')}", {}
            )
            semantic_score = float(semantic.get("score", 0.0) or 0.0)
            if not direct and keyword_score < 45 and semantic_score < 0.38:
                continue
            result = dict(item)
            result["keyword_score"] = round(keyword_score / 100.0, 4)
            result["match_score"] = round(
                max(keyword_score / 100.0, semantic_score), 4
            )
            if semantic_score:
                result["semantic_score"] = round(semantic_score, 4)
                result["matched_segment"] = semantic.get("segment")
            result["source"] = (
                "thought_trace"
                if item.get("thought_kind") == "trace"
                else "thought"
            )
            scored.append(result)
        scored.sort(
            key=lambda item: (
                float(item.get("match_score", 0.0)),
                str(item.get("last_seen") or ""),
            ),
            reverse=True,
        )
        return scored[: max(1, min(500, int(limit)))]

    async def is_read_only_trace(self, canonical_tag: str) -> bool:
        if not self.enabled:
            return False
        row = await asyncio.to_thread(self._get_thought_sync, canonical_tag)
        return bool(row and row.get("thought_kind") == "trace")

    def _get_thought_sync(self, canonical_tag: str) -> dict | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM xinchao_thoughts WHERE canonical_tag=?",
                (str(canonical_tag),),
            ).fetchone()
        return dict(row) if row else None

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
        """Return a repeatable read-only boot snapshot."""
        if not self.enabled:
            return {"available": False, "disabled": True}
        await self.settle_darkflow(mailbox_context=mailbox_context)
        preview = await asyncio.to_thread(self._preview_sync, beijing_now())
        darkflow = await self.pending_darkflow()
        if (
            darkflow
            and int(darkflow.get("cycle_id", -1)) == int(preview.get("cycle_id", -2))
        ):
            preview["darkflow"] = darkflow.get("content", "")
            preview["darkflow_item"] = darkflow
        else:
            preview["darkflow"] = ""
            preview["darkflow_item"] = None
        return preview

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
