"""Sole SQLite writer for Anima nursery stage 1."""

from __future__ import annotations

import base64
import json
import os
import sqlite3
import hashlib
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterator

from .creation_rules import blend_temperament, name_candidate_id, temperament_summary
from .models import (
    ActorContext,
    AuthSource,
    CaregiverRole,
    HealthOutcome,
    ModuleState,
    NurseryAction,
    NurseryCommand,
    NurseryError,
    OperationStatus,
    QuestionnaireKind,
    SafetyCategory,
    ensure_actor_allowed,
    ensure_action_allowed,
    state_after_action,
)
from .operations import (
    canonical_json,
    request_hash,
    validate_idempotency_key,
    validate_operation_id,
)
from .safety import sanitize_audit_metadata, validate_public_operation_payload
from .schemas import (
    EXPECTED_INDEXES,
    EXPECTED_TABLES,
    MIGRATION_1_CHECKSUM,
    MIGRATION_1_STATEMENTS,
    MIGRATION_2_CHECKSUM,
    MIGRATION_2_STATEMENTS,
    MIGRATION_3_CHECKSUM,
    MIGRATION_3_STATEMENTS,
    MIGRATION_4_CHECKSUM,
    MIGRATION_4_STATEMENTS,
    MIGRATION_5_CHECKSUM,
    MIGRATION_5_STATEMENTS,
    MIGRATION_6_CHECKSUM,
    MIGRATION_6_STATEMENTS,
    MIGRATION_7_CHECKSUM,
    MIGRATION_7_STATEMENTS,
    MIGRATION_8_CHECKSUM,
    MIGRATION_8_STATEMENTS,
    MIGRATION_9_CHECKSUM,
    MIGRATION_9_STATEMENTS,
    MIGRATION_10_CHECKSUM,
    MIGRATION_10_STATEMENTS,
    MIGRATION_11_CHECKSUM,
    MIGRATION_11_STATEMENTS,
    SCHEMA_VERSION,
)
from .care_rules import apply_completed_care, normalize_care_action


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def iso_time(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat(timespec="microseconds")


def parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


DEFAULT_CHILD_RUNTIME_STATE = {
    "thirst": 0.2,
    "hunger": 0.2,
    "fatigue": 0.2,
    "comfort": 0.7,
    "connection": 0.6,
    "play_drive": 0.5,
    "unwell": 0.0,
    "last_intent": "none",
    "emotion": {
        "primary": "安稳",
        "secondary": [],
        "valence": 0.35,
        "arousal": 0.3,
        "safety": 0.8,
        "cause": "",
    },
}


class NurseryStore:
    """All nursery writes and migrations pass through this object."""

    def __init__(
        self,
        db_path: str | os.PathLike[str],
        *,
        busy_timeout_ms: int = 5_000,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        self.db_path = str(Path(db_path).resolve())
        self.busy_timeout_ms = max(1, int(busy_timeout_ms))
        self.clock = clock
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    @classmethod
    def from_config(cls, config: dict[str, Any]) -> "NurseryStore":
        settings = config.get("nursery", {})
        db_path = settings.get("db_path") or os.environ.get("OMBRE_NURSERY_DB")
        if not db_path:
            db_path = os.path.join(config["buckets_dir"], "nursery.sqlite3")
        return cls(
            db_path,
            busy_timeout_ms=int(settings.get("busy_timeout_ms", 5_000)),
        )

    def _new_connection(self) -> sqlite3.Connection:
        connection = sqlite3.connect(
            self.db_path,
            timeout=self.busy_timeout_ms / 1000,
            isolation_level=None,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute(f"PRAGMA busy_timeout={self.busy_timeout_ms}")
        connection.execute("PRAGMA synchronous=NORMAL")
        return connection

    @contextmanager
    def _read(self) -> Iterator[sqlite3.Connection]:
        connection = self._new_connection()
        try:
            yield connection
        finally:
            connection.close()

    @contextmanager
    def _write(self) -> Iterator[sqlite3.Connection]:
        connection = self._new_connection()
        try:
            connection.execute("BEGIN IMMEDIATE")
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _initialize(self) -> None:
        with self._read() as connection:
            connection.execute("PRAGMA journal_mode=WAL")
        with self._write() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS nursery_schema_migrations (
                    schema_version INTEGER PRIMARY KEY,
                    applied_at TEXT NOT NULL,
                    checksum TEXT NOT NULL
                )
                """
            )
            rows = connection.execute(
                "SELECT schema_version, checksum FROM nursery_schema_migrations "
                "ORDER BY schema_version"
            ).fetchall()
            versions = {int(row["schema_version"]): str(row["checksum"]) for row in rows}
            if any(version > SCHEMA_VERSION for version in versions):
                raise NurseryError(
                    "SCHEMA_TOO_NEW", "nursery database schema is newer than this code"
                )
            migrations = {
                1: (MIGRATION_1_CHECKSUM, MIGRATION_1_STATEMENTS),
                2: (MIGRATION_2_CHECKSUM, MIGRATION_2_STATEMENTS),
                3: (MIGRATION_3_CHECKSUM, MIGRATION_3_STATEMENTS),
                4: (MIGRATION_4_CHECKSUM, MIGRATION_4_STATEMENTS),
                5: (MIGRATION_5_CHECKSUM, MIGRATION_5_STATEMENTS),
                6: (MIGRATION_6_CHECKSUM, MIGRATION_6_STATEMENTS),
                7: (MIGRATION_7_CHECKSUM, MIGRATION_7_STATEMENTS),
                8: (MIGRATION_8_CHECKSUM, MIGRATION_8_STATEMENTS),
                9: (MIGRATION_9_CHECKSUM, MIGRATION_9_STATEMENTS),
                10: (MIGRATION_10_CHECKSUM, MIGRATION_10_STATEMENTS),
                11: (MIGRATION_11_CHECKSUM, MIGRATION_11_STATEMENTS),
            }
            for version, (checksum, statements) in migrations.items():
                if version in versions and versions[version] != checksum:
                    raise NurseryError(
                        "MIGRATION_CHECKSUM_MISMATCH",
                        "nursery schema migration checksum does not match",
                        details={"schema_version": version},
                    )
                if version not in versions:
                    for statement in statements:
                        connection.execute(statement)
                    connection.execute(
                        "INSERT INTO nursery_schema_migrations "
                        "(schema_version, applied_at, checksum) VALUES (?, ?, ?)",
                        (version, iso_time(self.clock()), checksum),
                    )
        self.validate_schema()

    def validate_schema(self) -> None:
        with self._read() as connection:
            tables = {
                str(row["name"])
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
            }
            missing = EXPECTED_TABLES - tables
            if missing:
                raise NurseryError(
                    "SCHEMA_INCOMPLETE",
                    "nursery schema is missing required tables",
                    details={"tables": sorted(missing)},
                )
            indexes = {
                str(row["name"])
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='index'"
                ).fetchall()
            }
            missing_indexes = EXPECTED_INDEXES - indexes
            if missing_indexes:
                raise NurseryError(
                    "SCHEMA_INCOMPLETE",
                    "nursery schema is missing required indexes",
                    details={"indexes": sorted(missing_indexes)},
                )
            integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
            if integrity != "ok":
                raise NurseryError(
                    "DATABASE_INTEGRITY_FAILED", f"integrity_check returned: {integrity}"
                )

    def schema_version(self) -> int:
        with self._read() as connection:
            row = connection.execute(
                "SELECT MAX(schema_version) AS version FROM nursery_schema_migrations"
            ).fetchone()
        return int(row["version"] or 0)

    def backup_to(self, destination: str | os.PathLike[str]) -> str:
        target = Path(destination).resolve()
        target.parent.mkdir(parents=True, exist_ok=True)
        if target == Path(self.db_path):
            raise NurseryError("INVALID_BACKUP_TARGET", "backup target must be different")
        source_connection = self._new_connection()
        target_connection = sqlite3.connect(str(target))
        try:
            source_connection.backup(target_connection)
        finally:
            target_connection.close()
            source_connection.close()
        return str(target)

    def restore_copy_to(self, destination: str | os.PathLike[str]) -> str:
        """Restore through SQLite backup into a new path, including WAL content."""

        target = Path(destination).resolve()
        if target.exists():
            raise NurseryError("RESTORE_TARGET_EXISTS", "restore target already exists")
        target.parent.mkdir(parents=True, exist_ok=True)
        source_connection = self._new_connection()
        target_connection = sqlite3.connect(str(target))
        try:
            integrity = source_connection.execute("PRAGMA integrity_check").fetchone()[0]
            if integrity != "ok":
                raise NurseryError("DATABASE_INTEGRITY_FAILED", str(integrity))
            source_connection.backup(target_connection)
        finally:
            target_connection.close()
            source_connection.close()
        return str(target)

    def create_fixture(
        self,
        *,
        account_id: str,
        child_id: str,
        state: ModuleState = ModuleState.NEVER_ENABLED,
        stage_id: str = "infancy",
        state_version: int = 0,
    ) -> None:
        """Stage-1 test setup only; runtime creation is a stage-2 workflow."""

        stamp = iso_time(self.clock())
        with self._write() as connection:
            connection.execute(
                """
                INSERT INTO nursery_modules (
                    account_id, child_id, module_state, state_version,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    str(account_id),
                    str(child_id),
                    ModuleState(state).value,
                    int(state_version),
                    stamp,
                    stamp,
                ),
            )
            connection.execute(
                """
                INSERT INTO nursery_children (
                    child_id, account_id, stage_id, state_version,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    str(child_id),
                    str(account_id),
                    str(stage_id),
                    int(state_version),
                    stamp,
                    stamp,
                ),
            )

    def bootstrap_creation_draft(
        self,
        command: NurseryCommand,
        *,
        child_id: str,
        display_name: str,
        independent_temperament: dict[str, float],
        temperament_formula_version: str,
    ) -> dict[str, Any]:
        """Atomically create the first draft, fixed child id, user guardian, and trace."""

        if command.action != NurseryAction.START_DRAFT:
            raise NurseryError("INVALID_ACTION", "start_draft command is required")
        if command.actor.role != CaregiverRole.USER_GUARDIAN:
            raise NurseryError(
                "USER_GUARDIAN_REQUIRED", "only the user can start a creation draft"
            )
        ensure_actor_allowed(command.actor, command.action)
        validate_public_operation_payload(command.payload)
        validate_operation_id(command.operation_id)
        validate_idempotency_key(command.idempotency_key)
        if command.source is None:
            raise NurseryError("SOURCE_REQUIRED", "creation draft requires a source")
        if command.expected_state_version not in {None, 0}:
            raise NurseryError(
                "STALE_STATE_VERSION", "new creation draft must expect version zero"
            )
        if str(command.child_id) != str(child_id):
            raise NurseryError("INVALID_CHILD_ID", "command child id does not match draft")
        name = str(display_name).strip()
        if not name or len(name) > 80:
            raise NurseryError("INVALID_DISPLAY_NAME", "display name is required")
        digest = request_hash(command)
        stamp = iso_time(self.clock())
        with self._write() as connection:
            existing = connection.execute(
                """
                SELECT * FROM nursery_operations
                WHERE account_id=? AND caregiver_id=? AND idempotency_key=?
                """,
                (
                    command.actor.account_id,
                    command.actor.caregiver_id,
                    command.idempotency_key,
                ),
            ).fetchone()
            if existing:
                item = self._decode_operation(existing)
                if item["request_hash"] != digest:
                    raise NurseryError(
                        "IDEMPOTENCY_CONFLICT",
                        "creation idempotency key conflicts with saved request",
                    )
                return item
            collision = connection.execute(
                "SELECT * FROM nursery_operations WHERE operation_id=?",
                (command.operation_id,),
            ).fetchone()
            if collision:
                raise NurseryError(
                    "OPERATION_ID_CONFLICT", "operation id is already in use"
                )
            source_collision = connection.execute(
                """
                SELECT operation_id FROM nursery_source_events
                WHERE source_type=? AND source_id=? AND source_version=?
                """,
                (
                    command.source.source_type.value,
                    command.source.source_id,
                    command.source.source_version,
                ),
            ).fetchone()
            if source_collision:
                raise NurseryError(
                    "SOURCE_VERSION_CONFLICT", "creation source version is already used"
                )
            module = connection.execute(
                "SELECT child_id, module_state FROM nursery_modules WHERE account_id=?",
                (command.actor.account_id,),
            ).fetchone()
            if module:
                raise NurseryError(
                    "NURSERY_ALREADY_STARTED", "this account already has a nursery"
                )

            connection.execute(
                """
                INSERT INTO nursery_modules (
                    account_id, child_id, module_state, state_version,
                    created_at, updated_at
                ) VALUES (?, ?, 'draft', 1, ?, ?)
                """,
                (command.actor.account_id, child_id, stamp, stamp),
            )
            connection.execute(
                """
                INSERT INTO nursery_children (
                    child_id, account_id, stage_id, state_version,
                    created_at, updated_at
                ) VALUES (?, ?, 'infancy', 1, ?, ?)
                """,
                (child_id, command.actor.account_id, stamp, stamp),
            )
            connection.execute(
                """
                INSERT INTO nursery_caregivers (
                    caregiver_id, account_id, role, permission_status,
                    created_at, updated_at
                ) VALUES (?, ?, 'user_guardian', 'active', ?, ?)
                """,
                (command.actor.caregiver_id, command.actor.account_id, stamp, stamp),
            )
            connection.execute(
                """
                INSERT INTO nursery_caregiver_profiles (
                    caregiver_id, child_id, display_name, founding_guardian,
                    created_at, updated_at
                ) VALUES (?, ?, ?, 1, ?, ?)
                """,
                (command.actor.caregiver_id, child_id, name, stamp, stamp),
            )
            # The personal Anima MCP connection is the second founding
            # caregiver. It is represented immediately, but it still has to
            # complete its own questionnaire, naming, and confirmation work in
            # its own chat before this draft can become a child's active room.
            connection.execute(
                """
                INSERT INTO nursery_caregivers (
                    caregiver_id, account_id, role, permission_status,
                    created_at, updated_at
                ) VALUES ('anima-external-ai-guardian', ?, 'external_ai_guardian', 'active', ?, ?)
                """,
                (command.actor.account_id, stamp, stamp),
            )
            connection.execute(
                """
                INSERT INTO nursery_caregiver_profiles (
                    caregiver_id, child_id, display_name, founding_guardian,
                    created_at, updated_at
                ) VALUES ('anima-external-ai-guardian', ?, '外部 AI 养育者', 1, ?, ?)
                """,
                (child_id, stamp, stamp),
            )
            connection.execute(
                """
                INSERT INTO nursery_creation_drafts (
                    child_id, account_id, draft_version,
                    independent_temperament_json, temperament_formula_version,
                    created_at, updated_at
                ) VALUES (?, ?, 1, ?, ?, ?, ?)
                """,
                (
                    child_id,
                    command.actor.account_id,
                    canonical_json(independent_temperament),
                    str(temperament_formula_version),
                    stamp,
                    stamp,
                ),
            )
            result = {
                "action": command.action.value,
                "changed": True,
                "module_state": ModuleState.DRAFT.value,
                "state_version": 1,
                "draft_version": 1,
                "child_id": child_id,
            }
            connection.execute(
                """
                INSERT INTO nursery_operations (
                    operation_id, account_id, child_id, caregiver_id,
                    idempotency_key, request_hash, action, payload_json,
                    expected_state_version, before_state_version, status,
                    result_json, created_at, updated_at, started_at, completed_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0, 'completed', ?, ?, ?, ?, ?)
                """,
                (
                    command.operation_id,
                    command.actor.account_id,
                    child_id,
                    command.actor.caregiver_id,
                    command.idempotency_key,
                    digest,
                    command.action.value,
                    canonical_json(command.payload),
                    command.expected_state_version,
                    canonical_json(result),
                    stamp,
                    stamp,
                    stamp,
                    stamp,
                ),
            )
            connection.execute(
                """
                INSERT INTO nursery_state_snapshots (
                    operation_id, snapshot_kind, state_version, state_json, created_at
                ) VALUES (?, 'before', 0, ?, ?), (?, 'after', 1, ?, ?)
                """,
                (
                    command.operation_id,
                    canonical_json(
                        {"module_state": ModuleState.NEVER_ENABLED.value, "state_version": 0}
                    ),
                    stamp,
                    command.operation_id,
                    canonical_json(
                        {
                            "module_state": ModuleState.DRAFT.value,
                            "state_version": 1,
                            "stage_id": "infancy",
                            "active_pause_count": 0,
                        }
                    ),
                    stamp,
                ),
            )
            connection.execute(
                """
                INSERT INTO nursery_source_events (
                    account_id, child_id, operation_id, source_type,
                    source_id, source_version, specification_ref,
                    processing_status, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, 'applied', ?, ?)
                """,
                (
                    command.actor.account_id,
                    child_id,
                    command.operation_id,
                    command.source.source_type.value,
                    command.source.source_id,
                    command.source.source_version,
                    command.source.specification_ref,
                    stamp,
                    stamp,
                ),
            )
            row = connection.execute(
                "SELECT * FROM nursery_operations WHERE operation_id=?",
                (command.operation_id,),
            ).fetchone()
        return self._decode_operation(row)

    def register_caregiver(self, actor: ActorContext) -> None:
        stamp = iso_time(self.clock())
        with self._write() as connection:
            module = connection.execute(
                "SELECT account_id FROM nursery_modules WHERE account_id=?",
                (actor.account_id,),
            ).fetchone()
            if not module:
                raise NurseryError("NURSERY_NOT_FOUND", "nursery account was not found")
            existing = connection.execute(
                "SELECT account_id, role FROM nursery_caregivers WHERE caregiver_id=?",
                (actor.caregiver_id,),
            ).fetchone()
            if existing and (
                existing["account_id"] != actor.account_id
                or existing["role"] != actor.role.value
            ):
                raise NurseryError(
                    "CAREGIVER_ID_CONFLICT",
                    "caregiver_id is already bound to another scope or role",
                )
            connection.execute(
                """
                INSERT INTO nursery_caregivers (
                    caregiver_id, account_id, role, permission_status,
                    created_at, updated_at
                ) VALUES (?, ?, ?, 'active', ?, ?)
                ON CONFLICT(caregiver_id) DO UPDATE SET
                    permission_status='active', updated_at=excluded.updated_at
                """,
                (
                    actor.caregiver_id,
                    actor.account_id,
                    actor.role.value,
                    stamp,
                    stamp,
                ),
            )

    def assert_actor_registered(self, actor: ActorContext) -> None:
        with self._read() as connection:
            self._assert_actor(connection, actor)

    def ensure_personal_mcp_guardian(self) -> bool:
        """Idempotently backfill the personal Anima MCP guardian for one nursery.

        Earlier drafts predate the direct-MCP design. This migration only adds
        the canonical external guardian and its profile; it never changes the
        child, model connection, questionnaire answers, or Anima data.
        """

        stamp = iso_time(self.clock())
        with self._write() as connection:
            modules = connection.execute(
                """
                SELECT account_id, child_id, module_state
                FROM nursery_modules
                WHERE module_state IN ('draft', 'active', 'paused')
                ORDER BY created_at ASC
                LIMIT 2
                """
            ).fetchall()
            if len(modules) != 1:
                return False
            module = modules[0]
            existing = connection.execute(
                """
                SELECT 1 FROM nursery_caregivers c
                JOIN nursery_caregiver_profiles p ON p.caregiver_id=c.caregiver_id
                WHERE c.account_id=? AND c.role='external_ai_guardian'
                  AND c.permission_status='active' AND p.child_id=?
                  AND p.founding_guardian=1
                """,
                (module["account_id"], module["child_id"]),
            ).fetchone()
            if existing:
                return False
            collision = connection.execute(
                "SELECT account_id, role FROM nursery_caregivers WHERE caregiver_id='anima-external-ai-guardian'"
            ).fetchone()
            if collision and (
                collision["account_id"] != module["account_id"]
                or collision["role"] != CaregiverRole.EXTERNAL_AI_GUARDIAN.value
            ):
                raise NurseryError(
                    "CAREGIVER_ID_CONFLICT",
                    "the personal Anima MCP guardian is already bound elsewhere",
                )
            connection.execute(
                """
                INSERT INTO nursery_caregivers (
                    caregiver_id, account_id, role, permission_status, created_at, updated_at
                ) VALUES ('anima-external-ai-guardian', ?, 'external_ai_guardian', 'active', ?, ?)
                ON CONFLICT(caregiver_id) DO UPDATE SET
                    permission_status='active', updated_at=excluded.updated_at
                """,
                (module["account_id"], stamp, stamp),
            )
            connection.execute(
                """
                INSERT INTO nursery_caregiver_profiles (
                    caregiver_id, child_id, display_name, founding_guardian, created_at, updated_at
                ) VALUES ('anima-external-ai-guardian', ?, '外部 AI 养育者', 1, ?, ?)
                ON CONFLICT(caregiver_id) DO UPDATE SET
                    display_name=excluded.display_name, founding_guardian=1, updated_at=excluded.updated_at
                """,
                (module["child_id"], stamp, stamp),
            )
            if module["module_state"] == ModuleState.DRAFT.value:
                self._bump_draft_version(connection, str(module["child_id"]), stamp)
        return True

    def single_active_external_guardian(self) -> dict[str, str] | None:
        """Return the one founding external guardian for Anima's existing MCP entry.

        Anima's established MCP transport is a single personal connection, rather
        than a multi-account login service.  Nursery deliberately follows that
        boundary: direct MCP access is available only when exactly one active
        founding external guardian has been bound.  If this installation later
        becomes multi-tenant, it must add a real account resolver instead of
        guessing which guardian a request belongs to.
        """

        with self._read() as connection:
            rows = connection.execute(
                """
                SELECT c.account_id, c.caregiver_id
                FROM nursery_caregivers c
                JOIN nursery_caregiver_profiles p
                  ON p.caregiver_id=c.caregiver_id
                WHERE c.role='external_ai_guardian'
                  AND c.permission_status='active'
                  AND p.founding_guardian=1
                ORDER BY c.created_at ASC
                LIMIT 2
                """
            ).fetchall()
        if len(rows) != 1:
            return None
        return {
            "account_id": str(rows[0]["account_id"]),
            "caregiver_id": str(rows[0]["caregiver_id"]),
        }

    def child_id_for_account(self, account_id: str) -> str | None:
        """Return this personal Anima installation's only nursery child id."""

        with self._read() as connection:
            rows = connection.execute(
                """
                SELECT child_id FROM nursery_modules
                WHERE account_id=? AND module_state IN ('draft', 'active', 'paused')
                ORDER BY created_at ASC
                LIMIT 2
                """,
                (str(account_id),),
            ).fetchall()
        return str(rows[0]["child_id"]) if len(rows) == 1 else None

    def _assert_actor(self, connection: sqlite3.Connection, actor: ActorContext) -> None:
        row = connection.execute(
            """
            SELECT account_id, role, permission_status
            FROM nursery_caregivers WHERE caregiver_id=?
            """,
            (actor.caregiver_id,),
        ).fetchone()
        if not row or row["permission_status"] != "active":
            raise NurseryError("CAREGIVER_NOT_REGISTERED", "caregiver is not active")
        if row["account_id"] != actor.account_id or row["role"] != actor.role.value:
            raise NurseryError(
                "ACTOR_SCOPE_MISMATCH", "actor does not match registered caregiver scope"
            )

    @staticmethod
    def _decode_operation(row: sqlite3.Row | None) -> dict[str, Any] | None:
        if not row:
            return None
        result = dict(row)
        for json_key, output_key, default in (
            ("payload_json", "payload", {}),
            ("result_json", "result", {}),
        ):
            raw = result.pop(json_key, None)
            try:
                result[output_key] = json.loads(raw) if raw else default
            except (TypeError, ValueError):
                result[output_key] = default
        return result

    def get_operation(self, operation_id: str) -> dict[str, Any] | None:
        with self._read() as connection:
            row = connection.execute(
                "SELECT * FROM nursery_operations WHERE operation_id=?",
                (str(operation_id),),
            ).fetchone()
        return self._decode_operation(row)

    def get_operation_for_account(
        self, account_id: str, operation_id: str
    ) -> dict[str, Any] | None:
        """Return one operation only when it belongs to the requested account."""

        with self._read() as connection:
            row = connection.execute(
                """
                SELECT * FROM nursery_operations
                WHERE account_id=? AND operation_id=?
                """,
                (str(account_id), str(operation_id)),
            ).fetchone()
        return self._decode_operation(row)

    def get_account_overview(self, account_id: str) -> dict[str, Any]:
        """Side-effect-free one-child module gate used by the Anima adapter."""

        account = str(account_id).strip()
        if not account:
            raise NurseryError("INVALID_ACCOUNT", "account id is required")
        with self._read() as connection:
            rows = connection.execute(
                """
                SELECT m.account_id, m.child_id, m.module_state, m.state_version,
                       m.pre_delete_state, m.recycle_deadline, c.stage_id,
                       (SELECT COUNT(*) FROM nursery_pause_markers p
                        WHERE p.child_id=c.child_id AND p.released_at IS NULL)
                        AS active_pause_count
                FROM nursery_modules m
                JOIN nursery_children c ON c.child_id=m.child_id
                WHERE m.account_id=?
                ORDER BY m.created_at, m.child_id
                LIMIT 2
                """,
                (account,),
            ).fetchall()
        if not rows:
            return {
                "module": "nursery",
                "module_state": ModuleState.NEVER_ENABLED.value,
                "state_version": 0,
                "child_id": None,
                "stage_id": None,
                "active_pause_count": 0,
                "recycle_deadline": None,
                "restore_state": None,
            }
        if len(rows) > 1:
            raise NurseryError(
                "MULTIPLE_CHILDREN_NOT_SUPPORTED",
                "the first nursery version supports exactly one child per account",
            )
        row = rows[0]
        return {
            "module": "nursery",
            "module_state": str(row["module_state"]),
            "state_version": int(row["state_version"]),
            "child_id": str(row["child_id"]),
            "stage_id": str(row["stage_id"]),
            "active_pause_count": int(row["active_pause_count"]),
            "recycle_deadline": row["recycle_deadline"],
            "restore_state": row["pre_delete_state"],
        }

    def get_entry_preference(self, account_id: str) -> dict[str, Any]:
        """Read the account-level phone entry preference without creating a child."""

        account = str(account_id).strip()
        if not account:
            raise NurseryError("INVALID_ACCOUNT", "account id is required")
        with self._read() as connection:
            row = connection.execute(
                "SELECT preference, updated_at FROM nursery_entry_preferences WHERE account_id=?",
                (account,),
            ).fetchone()
        if row is None:
            return {"preference": "undecided", "updated_at": None}
        return {
            "preference": str(row["preference"]),
            "updated_at": str(row["updated_at"]),
        }

    def set_entry_preference(self, actor: ActorContext, preference: str) -> dict[str, Any]:
        """Save the owner-only entry choice independently of nursery creation."""

        if (
            actor.role != CaregiverRole.USER_GUARDIAN
            or actor.auth_source != AuthSource.MANAGER_SESSION
        ):
            raise NurseryError(
                "USER_GUARDIAN_REQUIRED",
                "only the signed-in account owner can change the nursery entry preference",
            )
        value = str(preference).strip().lower()
        if value not in {"visible", "hidden"}:
            raise NurseryError(
                "INVALID_ENTRY_PREFERENCE",
                "entry preference must be visible or hidden",
            )
        stamp = iso_time(self.clock())
        with self._write() as connection:
            connection.execute(
                """
                INSERT INTO nursery_entry_preferences (account_id, preference, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(account_id) DO UPDATE SET
                    preference=excluded.preference,
                    updated_at=excluded.updated_at
                """,
                (actor.account_id, value, stamp),
            )
        return {"preference": value, "updated_at": stamp}

    def get_state(self, account_id: str, child_id: str) -> dict[str, Any]:
        with self._read() as connection:
            row = connection.execute(
                """
                SELECT m.account_id, m.child_id, m.module_state, m.state_version,
                       m.pre_delete_state, m.recycle_deadline, c.stage_id,
                       (SELECT COUNT(*) FROM nursery_pause_markers p
                        WHERE p.child_id=c.child_id AND p.released_at IS NULL)
                        AS active_pause_count
                FROM nursery_modules m
                JOIN nursery_children c ON c.child_id=m.child_id
                WHERE m.account_id=? AND m.child_id=?
                """,
                (str(account_id), str(child_id)),
            ).fetchone()
        if not row:
            raise NurseryError("NURSERY_NOT_FOUND", "nursery child was not found")
        return dict(row)

    def get_state_for_child(self, child_id: str) -> dict[str, Any]:
        """Internal lookup used only by the Anima-to-nursery context bridge."""

        with self._read() as connection:
            row = connection.execute(
                """
                SELECT m.account_id, m.child_id, m.module_state, m.state_version,
                       m.pre_delete_state, m.recycle_deadline, c.stage_id,
                       (SELECT COUNT(*) FROM nursery_pause_markers p
                        WHERE p.child_id=c.child_id AND p.released_at IS NULL)
                        AS active_pause_count
                FROM nursery_modules m
                JOIN nursery_children c ON c.child_id=m.child_id
                WHERE m.child_id=?
                """,
                (str(child_id),),
            ).fetchone()
        if not row:
            raise NurseryError("NURSERY_NOT_FOUND", "nursery child was not found")
        return dict(row)

    def get_child_runtime_state(self, child_id: str) -> dict[str, Any]:
        """Read current child-only state without starting work or calling a model."""

        with self._read() as connection:
            row = connection.execute(
                "SELECT state_json, state_updated_at FROM nursery_child_runtime_state "
                "WHERE child_id=?",
                (str(child_id),),
            ).fetchone()
        if not row:
            return dict(DEFAULT_CHILD_RUNTIME_STATE)
        try:
            state = json.loads(str(row["state_json"]))
        except (TypeError, ValueError) as exc:
            raise NurseryError("INVALID_RUNTIME_STATE", "saved child state is invalid") from exc
        if not isinstance(state, dict):
            raise NurseryError("INVALID_RUNTIME_STATE", "saved child state is invalid")
        return dict(state)

    def get_body_clock(self, child_id: str) -> dict[str, Any]:
        """Read the saved body anchors; missing rows start at the current clock."""

        with self._read() as connection:
            row = connection.execute(
                "SELECT * FROM nursery_body_clocks WHERE child_id=?",
                (str(child_id),),
            ).fetchone()
        if row:
            return dict(row)
        stamp = iso_time(self.clock())
        return {
            "child_id": str(child_id),
            "last_fed_at": None,
            "last_hydrated_at": None,
            "sleep_started_at": None,
            "last_woke_at": None,
            "last_care_at": None,
            "settled_through": stamp,
            "rule_version": "body-time-disabled-v1",
            "frozen_at": None,
            "updated_at": stamp,
        }

    @staticmethod
    def _body_clock_payload(
        child_id: str,
        *,
        stamp: str,
        rule_version: str,
        frozen_at: str | None = None,
    ) -> dict[str, Any]:
        return {
            "child_id": str(child_id),
            "last_fed_at": None,
            "last_hydrated_at": None,
            "sleep_started_at": None,
            "last_woke_at": None,
            "last_care_at": None,
            "settled_through": stamp,
            "rule_version": rule_version,
            "frozen_at": frozen_at,
            "updated_at": stamp,
        }

    @staticmethod
    def _save_body_clock(
        connection: sqlite3.Connection,
        clock: dict[str, Any],
    ) -> None:
        connection.execute(
            """
            INSERT INTO nursery_body_clocks (
                child_id, last_fed_at, last_hydrated_at, sleep_started_at,
                last_woke_at, last_care_at, settled_through, rule_version,
                frozen_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(child_id) DO UPDATE SET
                last_fed_at=excluded.last_fed_at,
                last_hydrated_at=excluded.last_hydrated_at,
                sleep_started_at=excluded.sleep_started_at,
                last_woke_at=excluded.last_woke_at,
                last_care_at=excluded.last_care_at,
                settled_through=excluded.settled_through,
                rule_version=excluded.rule_version,
                frozen_at=excluded.frozen_at,
                updated_at=excluded.updated_at
            """,
            tuple(
                clock.get(name)
                for name in (
                    "child_id",
                    "last_fed_at",
                    "last_hydrated_at",
                    "sleep_started_at",
                    "last_woke_at",
                    "last_care_at",
                    "settled_through",
                    "rule_version",
                    "frozen_at",
                    "updated_at",
                )
            ),
        )

    @staticmethod
    def _runtime_from_row(state_json: Any) -> dict[str, Any]:
        if isinstance(state_json, dict):
            return dict(state_json)
        try:
            runtime = (
                json.loads(str(state_json))
                if state_json
                else dict(DEFAULT_CHILD_RUNTIME_STATE)
            )
        except (TypeError, ValueError) as exc:
            raise NurseryError("INVALID_RUNTIME_STATE", "saved child state is invalid") from exc
        if not isinstance(runtime, dict):
            raise NurseryError("INVALID_RUNTIME_STATE", "saved child state is invalid")
        return dict(runtime)

    @classmethod
    def _project_body_settlement(
        cls,
        *,
        child_id: str,
        module_state: str,
        state_json: Any,
        existing_clock: sqlite3.Row | dict[str, Any] | None,
        through: datetime,
        rule: Any,
    ) -> dict[str, Any]:
        stamp = iso_time(through)
        clock = (
            dict(existing_clock)
            if existing_clock
            else cls._body_clock_payload(
                child_id, stamp=stamp, rule_version=rule.version
            )
        )
        expected_settled_through = str(clock["settled_through"])
        runtime = cls._runtime_from_row(state_json)
        expected_runtime_state = dict(runtime)
        settled = parse_time(clock.get("settled_through")) or through
        if module_state != ModuleState.ACTIVE.value or through <= settled:
            return {
                "runtime_state": runtime,
                "clock": clock,
                "expected_settled_through": expected_settled_through,
                "expected_runtime_state": expected_runtime_state,
            }
        from .body_state import project_life_state
        runtime, clock = project_life_state(runtime, clock, evaluated_at=through,
            rule=rule, active=module_state == ModuleState.ACTIVE.value)
        # Body time is independent of health and must preserve the saved value.
        runtime["unwell"] = float(runtime.get("unwell", 0.0))
        clock.update(
            {
                "settled_through": stamp,
                "rule_version": rule.version,
                "frozen_at": None,
                "updated_at": stamp,
            }
        )
        return {
            "runtime_state": runtime,
            "clock": clock,
            "expected_settled_through": expected_settled_through,
            "expected_runtime_state": expected_runtime_state,
        }

    def preview_body_settlement(
        self,
        *,
        child_id: str,
        through: datetime,
        rule: Any,
    ) -> dict[str, Any]:
        """Project a settlement for a pending write without changing storage."""

        from .body_state import BodyTimeRule

        if not isinstance(rule, BodyTimeRule):
            raise NurseryError("INVALID_BODY_RULE", "a BodyTimeRule is required")
        try:
            rule.validate()
        except ValueError as exc:
            raise NurseryError("INVALID_BODY_RULE", str(exc)) from exc
        through = through if through.tzinfo else through.replace(tzinfo=timezone.utc)
        through = through.astimezone(timezone.utc)
        with self._read() as connection:
            state = connection.execute(
                """
                SELECT m.module_state, r.state_json
                FROM nursery_modules m
                LEFT JOIN nursery_child_runtime_state r ON r.child_id=m.child_id
                WHERE m.child_id=?
                """,
                (str(child_id),),
            ).fetchone()
            if not state:
                raise NurseryError("NURSERY_NOT_FOUND", "nursery child was not found")
            existing = connection.execute(
                "SELECT * FROM nursery_body_clocks WHERE child_id=?",
                (str(child_id),),
            ).fetchone()
        return self._project_body_settlement(
            child_id=str(child_id),
            module_state=str(state["module_state"]),
            state_json=state["state_json"],
            existing_clock=existing,
            through=through,
            rule=rule,
        )

    def settle_body_time(
        self,
        *,
        child_id: str,
        through: datetime,
        rule: Any,
    ) -> dict[str, Any]:
        """Idempotently consume one time interval before a real child write."""

        from .body_state import BodyTimeRule

        if not isinstance(rule, BodyTimeRule):
            raise NurseryError("INVALID_BODY_RULE", "a BodyTimeRule is required")
        try:
            rule.validate()
        except ValueError as exc:
            raise NurseryError("INVALID_BODY_RULE", str(exc)) from exc
        through = through if through.tzinfo else through.replace(tzinfo=timezone.utc)
        through = through.astimezone(timezone.utc)
        stamp = iso_time(through)
        with self._write() as connection:
            state = connection.execute(
                """
                SELECT m.module_state, r.state_json
                FROM nursery_modules m
                LEFT JOIN nursery_child_runtime_state r ON r.child_id=m.child_id
                WHERE m.child_id=?
                """,
                (str(child_id),),
            ).fetchone()
            if not state:
                raise NurseryError("NURSERY_NOT_FOUND", "nursery child was not found")
            existing = connection.execute(
                "SELECT * FROM nursery_body_clocks WHERE child_id=?",
                (str(child_id),),
            ).fetchone()
            settlement = self._project_body_settlement(
                child_id=str(child_id),
                module_state=str(state["module_state"]),
                state_json=state["state_json"],
                existing_clock=existing,
                through=through,
                rule=rule,
            )
            runtime = settlement["runtime_state"]
            clock = settlement["clock"]
            if state["module_state"] != ModuleState.ACTIVE.value:
                return clock
            if existing and str(clock["settled_through"]) == str(
                existing["settled_through"]
            ):
                return dict(existing)
            connection.execute(
                """
                INSERT INTO nursery_child_runtime_state (child_id, state_json, state_updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(child_id) DO UPDATE SET
                    state_json=excluded.state_json, state_updated_at=excluded.state_updated_at
                """,
                (str(child_id), canonical_json(runtime), stamp),
            )
            self._save_body_clock(connection, clock)
            saved = connection.execute(
                "SELECT * FROM nursery_body_clocks WHERE child_id=?",
                (str(child_id),),
            ).fetchone()
        return dict(saved)

    def get_short_event_by_operation(self, operation_id: str) -> dict[str, Any] | None:
        with self._read() as connection:
            row = connection.execute(
                "SELECT event_id, child_id, caregiver_id, event_kind, conversation_channel, "
                "caregiver_message, child_reply, intent, created_at, expires_at "
                "FROM nursery_child_short_events WHERE operation_id=?",
                (str(operation_id),),
            ).fetchone()
        return dict(row) if row else None

    def get_shared_care_event_by_operation(self, operation_id: str) -> dict[str, Any] | None:
        """Return a retained care fact linked to one interaction, never its raw chat."""

        with self._read() as connection:
            row = connection.execute(
                """
                SELECT e.care_event_id, e.caregiver_id, e.conversation_channel,
                       e.category, e.object_name, e.summary, e.world_item_id, e.state_before_json,
                       e.state_after_json, e.created_at, e.correction_json,
                       p.display_name AS caregiver_display_name
                FROM nursery_shared_care_events e
                LEFT JOIN nursery_caregiver_profiles p
                  ON p.caregiver_id=e.caregiver_id AND p.child_id=e.child_id
                WHERE e.source_operation_id=? AND e.created_at>?
                """,
                (str(operation_id), iso_time(self.clock() - timedelta(days=30))),
            ).fetchone()
        if not row:
            return None
        result = dict(row)
        result["state_before"] = self._json_value(result.pop("state_before_json"), {})
        result["state_after"] = self._json_value(result.pop("state_after_json"), {})
        result["correction"] = self._json_value(result.pop("correction_json"), None)
        result["caregiver_display_name"] = result.get("caregiver_display_name") or "一位养育者"
        return result

    def get_caregiver_identity(
        self, *, actor: ActorContext, child_id: str
    ) -> dict[str, str]:
        """Read the profile name used for child-facing language, after scope checks."""

        with self._read() as connection:
            self._assert_actor(connection, actor)
            row = connection.execute(
                """
                SELECT display_name FROM nursery_caregiver_profiles
                WHERE caregiver_id=? AND child_id=?
                """,
                (actor.caregiver_id, str(child_id)),
            ).fetchone()
        return {
            "display_name": str(row["display_name"]) if row else "一位养育者",
            "role": actor.role.value,
        }

    def list_recent_child_interactions(
        self,
        child_id: str,
        *,
        conversation_channel: str,
        caregiver_id: str,
        limit: int = 8,
    ) -> list[dict[str, str]]:
        """Return the retained conversation tail for the next child reply.

        This remains bounded by the short-event retention window.  It is used
        only as temporary dialogue continuity and never promotes a turn into
        Anima's long-term memory.
        """

        count = max(1, min(int(limit), 12))
        stamp = iso_time(self.clock())
        with self._read() as connection:
            rows = connection.execute(
                """
                SELECT caregiver_message, child_reply, intent, created_at
                FROM nursery_child_short_events
                WHERE child_id=? AND caregiver_id=? AND conversation_channel=?
                  AND event_kind='interaction' AND expires_at>?
                ORDER BY created_at DESC, event_id DESC
                LIMIT ?
                """,
                (str(child_id), str(caregiver_id), str(conversation_channel), stamp, count),
            ).fetchall()
        return [
            {
                "caregiver_message": str(row["caregiver_message"]),
                "child_reply": str(row["child_reply"]),
                "intent": str(row["intent"]),
                "created_at": str(row["created_at"]),
            }
            for row in reversed(rows)
        ]

    def save_child_safe_anima_context(
        self, *, child_id: str, context: dict[str, Any], retention_days: int = 30
    ) -> dict[str, Any]:
        """Persist only a pre-trimmed Anima summary for a later child turn."""

        with self._write() as connection:
            return self._save_child_safe_anima_context_in_transaction(
                connection,
                child_id=child_id,
                context=context,
                retention_days=retention_days,
            )

    def _save_child_safe_anima_context_in_transaction(
        self,
        connection: sqlite3.Connection,
        *,
        child_id: str,
        context: dict[str, Any],
        retention_days: int = 30,
    ) -> dict[str, Any]:
        """Save the already validated context within the caller's transaction."""

        source_key = str(context.get("source_key") or "").strip()
        source_version = str(context.get("source_version") or "").strip()
        category = str(context.get("category") or "").strip()
        summary = str(context.get("summary") or "").strip()
        occurred_at = str(context.get("occurred_at") or "").strip()
        disposition = context.get("disposition")
        if not source_key or not source_version or not category or not summary or not occurred_at:
            raise NurseryError("INVALID_ANIMA_CONTEXT", "child-safe Anima context is incomplete")
        if not isinstance(disposition, dict):
            raise NurseryError("INVALID_ANIMA_CONTEXT", "Anima disposition context is invalid")
        stamp = iso_time(self.clock())
        expiry = iso_time(self.clock() + timedelta(days=max(1, int(retention_days))))
        context_id = "anima:" + hashlib.sha256(
            f"{child_id}\0{source_key}\0{source_version}".encode("utf-8")
        ).hexdigest()[:48]
        child = connection.execute(
            """
            SELECT m.module_state FROM nursery_modules m
            WHERE m.child_id=?
            """,
            (str(child_id),),
        ).fetchone()
        if not child or child["module_state"] != ModuleState.ACTIVE.value:
            raise NurseryError(
                "NURSERY_NOT_ACTIVE", "Anima summaries are accepted only for an active child"
            )
        inserted = connection.execute(
            """
            INSERT OR IGNORE INTO nursery_anima_child_contexts (
                context_id, child_id, source_key, source_version, category,
                summary, occurred_at, disposition_json, created_at, expires_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                context_id,
                str(child_id),
                source_key,
                source_version,
                category,
                summary,
                occurred_at,
                canonical_json(disposition),
                stamp,
                expiry,
            ),
        ).rowcount
        return {"context_id": context_id, "accepted": bool(inserted), "expires_at": expiry}

    def list_active_child_safe_anima_contexts(
        self, child_id: str, *, limit: int = 3
    ) -> list[dict[str, Any]]:
        stamp = iso_time(self.clock())
        with self._read() as connection:
            rows = connection.execute(
                """
                SELECT context_id, category, summary, occurred_at, disposition_json
                FROM nursery_anima_child_contexts
                WHERE child_id=? AND expires_at>? AND source_validity='valid'
                ORDER BY occurred_at DESC, context_id DESC LIMIT ?
                """,
                (str(child_id), stamp, max(1, min(6, int(limit)))),
            ).fetchall()
        result = []
        for row in rows:
            try:
                disposition = json.loads(str(row["disposition_json"]))
            except (TypeError, ValueError):
                disposition = {}
            result.append(
                {
                    "context_id": str(row["context_id"]),
                    "category": str(row["category"]),
                    "summary": str(row["summary"]),
                    "occurred_at": str(row["occurred_at"]),
                    "disposition": disposition if isinstance(disposition, dict) else {},
                }
            )
        return result

    def create_external_mcp_grant(
        self,
        *,
        grant_id: str,
        account_id: str,
        child_id: str,
        caregiver_id: str,
        token_hash: str,
        permissions: tuple[str, ...],
        issued_by_caregiver_id: str,
        issued_at: str,
        expires_at: str,
    ) -> None:
        """Persist only a revocable token fingerprint, never the bearer token."""

        with self._write() as connection:
            child = connection.execute(
                "SELECT account_id FROM nursery_modules WHERE account_id=? AND child_id=?",
                (str(account_id), str(child_id)),
            ).fetchone()
            if not child:
                raise NurseryError("NURSERY_NOT_FOUND", "nursery child was not found")
            external = connection.execute(
                """
                SELECT role, permission_status FROM nursery_caregivers
                WHERE caregiver_id=? AND account_id=?
                """,
                (str(caregiver_id), str(account_id)),
            ).fetchone()
            issuer = connection.execute(
                """
                SELECT role, permission_status FROM nursery_caregivers
                WHERE caregiver_id=? AND account_id=?
                """,
                (str(issued_by_caregiver_id), str(account_id)),
            ).fetchone()
            if (
                not external
                or external["role"] != CaregiverRole.EXTERNAL_AI_GUARDIAN.value
                or external["permission_status"] != "active"
            ):
                raise NurseryError(
                    "EXTERNAL_GUARDIAN_NOT_BOUND",
                    "external AI guardian must be actively bound before authorization",
                )
            if (
                not issuer
                or issuer["role"] != CaregiverRole.USER_GUARDIAN.value
                or issuer["permission_status"] != "active"
            ):
                raise NurseryError("USER_GUARDIAN_REQUIRED", "user guardian authorization is required")
            connection.execute(
                """
                INSERT INTO nursery_external_mcp_grants (
                    grant_id, account_id, child_id, caregiver_id, token_hash,
                    permissions_json, issued_by_caregiver_id, issued_at, expires_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    str(grant_id),
                    str(account_id),
                    str(child_id),
                    str(caregiver_id),
                    str(token_hash),
                    canonical_json(list(permissions)),
                    str(issued_by_caregiver_id),
                    str(issued_at),
                    str(expires_at),
                ),
            )

    def resolve_external_mcp_grant(self, *, grant_id: str, token_hash: str) -> dict[str, Any]:
        stamp = iso_time(self.clock())
        with self._read() as connection:
            row = connection.execute(
                """
                SELECT account_id, child_id, caregiver_id, permissions_json,
                       expires_at, revoked_at
                FROM nursery_external_mcp_grants
                WHERE grant_id=? AND token_hash=?
                """,
                (str(grant_id), str(token_hash)),
            ).fetchone()
        if not row or row["revoked_at"] or str(row["expires_at"]) <= stamp:
            raise NurseryError("EXTERNAL_MCP_GRANT_INVALID", "external MCP authorization is invalid")
        try:
            permissions = json.loads(str(row["permissions_json"]))
        except (TypeError, ValueError) as exc:
            raise NurseryError("EXTERNAL_MCP_GRANT_INVALID", "external MCP authorization is invalid") from exc
        if not isinstance(permissions, list):
            raise NurseryError("EXTERNAL_MCP_GRANT_INVALID", "external MCP authorization is invalid")
        return {
            "account_id": str(row["account_id"]),
            "child_id": str(row["child_id"]),
            "caregiver_id": str(row["caregiver_id"]),
            "permissions": [str(item) for item in permissions if str(item).strip()],
        }

    def revoke_external_mcp_grant(
        self, *, grant_id: str, user_actor: ActorContext
    ) -> bool:
        """User-only revocation for a previously issued external MCP grant."""

        self.assert_actor_registered(user_actor)
        if user_actor.role != CaregiverRole.USER_GUARDIAN:
            raise NurseryError("USER_GUARDIAN_REQUIRED", "user guardian authorization is required")
        with self._write() as connection:
            changed = connection.execute(
                """
                UPDATE nursery_external_mcp_grants SET revoked_at=?
                WHERE grant_id=? AND account_id=? AND revoked_at IS NULL
                """,
                (iso_time(self.clock()), str(grant_id), user_actor.account_id),
            ).rowcount
        return bool(changed)

    def purge_expired_child_short_events(self) -> dict[str, int]:
        """Hard-delete 30-day dialogue without erasing body or health facts."""

        now = self.clock()
        stamp = iso_time(now)
        with self._write() as connection:
            deleted_care = connection.execute(
                "DELETE FROM nursery_shared_care_events WHERE created_at<=?",
                (iso_time(now - timedelta(days=30)),),
            ).rowcount
            deleted = connection.execute(
                "DELETE FROM nursery_child_short_events WHERE expires_at<=?", (stamp,)
            ).rowcount
            cutoff = iso_time(now - timedelta(days=30))
            connection.execute("DELETE FROM nursery_family_events WHERE created_at<=?", (cutoff,))
            connection.execute("DELETE FROM nursery_family_threads WHERE updated_at<=?", (cutoff,))
            connection.execute("DELETE FROM nursery_relationships WHERE updated_at<=?", (cutoff,))
            connection.execute("DELETE FROM nursery_growth_observations WHERE created_at<=?", (cutoff,))
            # Retain idempotency receipts/hashes, not expired family text in operation copies.
            connection.execute(
                """UPDATE nursery_operations SET payload_json='{}',result_json='{"expired":true}'
                WHERE action IN ('update_family_thread','record_relationship_event','set_rest_state','correct_care_event','record_growth_observation')
                AND status IN ('completed','rejected','cancelled') AND updated_at<=?""", (cutoff,)
            )
            deleted_contexts = connection.execute(
                "DELETE FROM nursery_anima_child_contexts WHERE expires_at<=?", (stamp,)
            ).rowcount
            # Runtime state has no per-field provenance, so reset only the
            # short-lived emotional residue while retaining durable need and
            # health fields.  This is intentionally a bounded cleanup, not a
            # medical or body-state mutation.
            stale = connection.execute(
                """
                SELECT child_id, state_json FROM nursery_child_runtime_state
                WHERE state_updated_at<=?
                  AND NOT EXISTS (
                      SELECT 1 FROM nursery_child_short_events e
                      WHERE e.child_id=nursery_child_runtime_state.child_id
                        AND e.expires_at>?
                  )
                """,
                (cutoff, stamp),
            ).fetchall()
            reset = 0
            for row in stale:
                runtime = self._runtime_from_row(row["state_json"])
                cleaned = dict(DEFAULT_CHILD_RUNTIME_STATE)
                for field in ("thirst", "hunger", "fatigue", "unwell"):
                    cleaned[field] = runtime.get(field, cleaned[field])
                if canonical_json(cleaned) != canonical_json(runtime):
                    connection.execute(
                        """
                        UPDATE nursery_child_runtime_state
                        SET state_json=?, state_updated_at=? WHERE child_id=?
                        """,
                        (canonical_json(cleaned), stamp, row["child_id"]),
                    )
                    reset += 1
        return {
            "deleted_events": int(deleted),
            "deleted_care_events": int(deleted_care),
            "deleted_anima_contexts": int(deleted_contexts),
            "reset_states": int(reset),
        }

    def get_model_config_internal(self, account_id: str) -> dict[str, Any] | None:
        """Internal vault-maintenance view; adapters must never expose it."""

        with self._read() as connection:
            row = connection.execute(
                "SELECT * FROM nursery_model_configs WHERE account_id=?",
                (str(account_id),),
            ).fetchone()
        if not row:
            return None
        result = dict(row)
        result["capabilities"] = json.loads(result.pop("capabilities_json"))
        return result

    def get_private_submission_ref(
        self,
        *,
        child_id: str,
        caregiver_id: str,
        questionnaire_kind: str,
    ) -> str | None:
        with self._read() as connection:
            row = connection.execute(
                """
                SELECT private_ref FROM nursery_private_submissions
                WHERE child_id=? AND caregiver_id=? AND questionnaire_kind=?
                """,
                (str(child_id), str(caregiver_id), str(questionnaire_kind)),
            ).fetchone()
        return str(row["private_ref"]) if row else None

    def private_refs_for_child(self, child_id: str) -> list[str]:
        with self._read() as connection:
            rows = connection.execute(
                """
                SELECT private_ref FROM nursery_private_submissions WHERE child_id=?
                """,
                (str(child_id),),
            ).fetchall()
        return [str(row["private_ref"]) for row in rows]

    def get_creation_draft_view(
        self,
        *,
        actor: ActorContext,
        child_id: str,
    ) -> dict[str, Any]:
        """Shared creation view with questionnaire vectors and vault refs removed."""

        with self._read() as connection:
            self._assert_actor(connection, actor)
            if actor.role not in {
                CaregiverRole.USER_GUARDIAN,
                CaregiverRole.EXTERNAL_AI_GUARDIAN,
            }:
                raise NurseryError(
                    "ACTOR_NOT_GUARDIAN", "creation draft is limited to its guardians"
                )
            founding = connection.execute(
                """
                SELECT founding_guardian FROM nursery_caregiver_profiles
                WHERE caregiver_id=? AND child_id=?
                """,
                (actor.caregiver_id, str(child_id)),
            ).fetchone()
            if not founding or not int(founding["founding_guardian"]):
                raise NurseryError(
                    "CREATION_DRAFT_FORBIDDEN",
                    "only founding guardians can read the creation draft",
                )
            draft = connection.execute(
                """
                SELECT child_id, account_id, draft_version, child_kind,
                       sex_status, stage_id, selected_candidate_id,
                       official_name, nickname, address_terms_json,
                       initial_space_json, initial_space_completed,
                       draft_status, created_at, updated_at, sealed_at
                FROM nursery_creation_drafts
                WHERE account_id=? AND child_id=?
                """,
                (actor.account_id, str(child_id)),
            ).fetchone()
            if not draft:
                raise NurseryError("DRAFT_NOT_FOUND", "creation draft was not found")
            caregivers = connection.execute(
                """
                SELECT c.caregiver_id, c.role, c.permission_status,
                       p.display_name, p.founding_guardian
                FROM nursery_caregivers c
                JOIN nursery_caregiver_profiles p ON p.caregiver_id=c.caregiver_id
                WHERE c.account_id=? AND p.child_id=?
                ORDER BY c.role, c.caregiver_id
                """,
                (actor.account_id, str(child_id)),
            ).fetchall()
            candidates = connection.execute(
                """
                SELECT candidate_id, caregiver_id, ordinal, proposed_name,
                       meaning_text, sound_notes, avoid_notes
                FROM nursery_name_candidates WHERE child_id=?
                ORDER BY caregiver_id, ordinal
                """,
                (str(child_id),),
            ).fetchall()
            preferences = connection.execute(
                """
                SELECT reviewer_id, candidate_id, preference
                FROM nursery_name_preferences WHERE child_id=?
                ORDER BY reviewer_id, candidate_id
                """,
                (str(child_id),),
            ).fetchall()
            submissions = connection.execute(
                """
                SELECT caregiver_id, questionnaire_kind, questionnaire_version,
                       submitted_at
                FROM nursery_private_submissions WHERE child_id=?
                ORDER BY caregiver_id, questionnaire_kind
                """,
                (str(child_id),),
            ).fetchall()
            temperament = connection.execute(
                """
                SELECT formula_version, summary_text, calculated_at, sealed_at
                FROM nursery_temperament_profiles WHERE child_id=?
                """,
                (str(child_id),),
            ).fetchone()
            model = connection.execute(
                """
                SELECT provider_id, base_url, model_name, credential_suffix,
                       connection_status, capabilities_json, config_version, tested_at
                FROM nursery_model_configs WHERE account_id=?
                """,
                (actor.account_id,),
            ).fetchone()
            confirmations = connection.execute(
                """
                SELECT caregiver_id, subject_version, confirmation_status, updated_at
                FROM nursery_confirmations
                WHERE subject_type='child_creation' AND subject_id=?
                ORDER BY caregiver_id
                """,
                (str(child_id),),
            ).fetchall()
            missing = self._creation_readiness(
                connection, account_id=actor.account_id, child_id=str(child_id)
            )
        view = dict(draft)
        view["address_terms"] = json.loads(view.pop("address_terms_json"))
        view["initial_space"] = json.loads(view.pop("initial_space_json"))
        view["initial_space_completed"] = bool(view["initial_space_completed"])
        view["caregivers"] = [dict(row) for row in caregivers]
        for item in view["caregivers"]:
            item["founding_guardian"] = bool(item["founding_guardian"])
        view["name_candidates"] = [dict(row) for row in candidates]
        view["name_preferences"] = [dict(row) for row in preferences]
        view["questionnaire_progress"] = [dict(row) for row in submissions]
        view["temperament"] = dict(temperament) if temperament else None
        view["model_connection"] = dict(model) if model else None
        if view["model_connection"]:
            view["model_connection"]["capabilities"] = json.loads(
                view["model_connection"].pop("capabilities_json")
            )
        view["creation_confirmations"] = [dict(row) for row in confirmations]
        view["missing_requirements"] = missing
        return view

    def get_child_identity(self, child_id: str) -> dict[str, Any] | None:
        with self._read() as connection:
            row = connection.execute(
                "SELECT * FROM nursery_child_identity WHERE child_id=?",
                (str(child_id),),
            ).fetchone()
        if not row:
            return None
        result = dict(row)
        result["address_terms"] = json.loads(result.pop("address_terms_json"))
        result["temperament"] = json.loads(result.pop("temperament_json"))
        return result

    def relationships_for(self, child_id: str) -> list[dict[str, Any]]:
        with self._read() as connection:
            rows = connection.execute(
                """
                SELECT child_id, caregiver_id, trust, familiarity, closeness, repair
                FROM nursery_relationships WHERE child_id=? ORDER BY caregiver_id
                """,
                (str(child_id),),
            ).fetchall()
        return [dict(row) for row in rows]

    def lifecycle_events_for(self, child_id: str) -> list[dict[str, Any]]:
        with self._read() as connection:
            rows = connection.execute(
                """
                SELECT event_id, account_id, child_id, event_type, operation_id,
                       event_json, sync_status, created_at
                FROM nursery_lifecycle_events WHERE child_id=? ORDER BY event_id
                """,
                (str(child_id),),
            ).fetchall()
        result: list[dict[str, Any]] = []
        for row in rows:
            item = dict(row)
            item["event"] = json.loads(item.pop("event_json"))
            result.append(item)
        return result

    def register_operation(
        self,
        command: NurseryCommand,
        request_digest: str,
    ) -> dict[str, Any]:
        stamp = iso_time(self.clock())
        scope = (
            command.actor.account_id,
            command.child_id,
            command.actor.caregiver_id,
            command.idempotency_key,
        )
        with self._write() as connection:
            internal_anima_sync = (
                command.action
                in {
                    NurseryAction.SYNC_ANIMA_CONTEXT,
                    NurseryAction.APPLY_ANIMA_FAMILY_EVENT,
                }
                and command.actor.role == CaregiverRole.SYSTEM_EVENT
                and command.actor.auth_source == AuthSource.INTERNAL_EVENT
            )
            if not internal_anima_sync:
                self._assert_actor(connection, command.actor)
            else:
                # The memory-management actor is an internal audit principal,
                # not a caregiver. It exists only to satisfy the immutable
                # operation foreign key for this one allowlisted action.
                existing_internal = connection.execute(
                    "SELECT account_id, role FROM nursery_caregivers WHERE caregiver_id=?",
                    (command.actor.caregiver_id,),
                ).fetchone()
                if existing_internal and (
                    existing_internal["account_id"] != command.actor.account_id
                    or existing_internal["role"] != CaregiverRole.SYSTEM_EVENT.value
                ):
                    raise NurseryError(
                        "ANIMA_CONTEXT_ACTOR_CONFLICT",
                        "internal Anima actor has an incompatible saved scope",
                    )
                if not existing_internal:
                    connection.execute(
                        """
                        INSERT INTO nursery_caregivers (
                            caregiver_id, account_id, role, permission_status,
                            created_at, updated_at
                        ) VALUES (?, ?, 'system_event', 'active', ?, ?)
                        """,
                        (
                            command.actor.caregiver_id,
                            command.actor.account_id,
                            stamp,
                            stamp,
                        ),
                    )
            state = connection.execute(
                """
                SELECT child_id, module_state FROM nursery_modules
                WHERE account_id=? AND child_id=?
                """,
                (command.actor.account_id, command.child_id),
            ).fetchone()
            if not state:
                raise NurseryError("NURSERY_NOT_FOUND", "nursery child was not found")

            existing = connection.execute(
                """
                SELECT * FROM nursery_operations
                WHERE account_id=? AND child_id=? AND caregiver_id=?
                  AND idempotency_key=?
                """,
                scope,
            ).fetchone()
            if existing:
                item = self._decode_operation(existing)
                if item["request_hash"] != request_digest:
                    return {"decision": "conflict", "code": "IDEMPOTENCY_CONFLICT"}
                return {"decision": "replay", "operation": item}

            operation_collision = connection.execute(
                "SELECT * FROM nursery_operations WHERE operation_id=?",
                (command.operation_id,),
            ).fetchone()
            if operation_collision:
                return {"decision": "conflict", "code": "OPERATION_ID_CONFLICT"}

            if command.source:
                source_collision = connection.execute(
                    """
                    SELECT o.* FROM nursery_source_events s
                    JOIN nursery_operations o ON o.operation_id=s.operation_id
                    WHERE s.source_type=? AND s.source_id=? AND s.source_version=?
                    """,
                    (
                        command.source.source_type.value,
                        command.source.source_id,
                        command.source.source_version,
                    ),
                ).fetchone()
                if source_collision:
                    item = self._decode_operation(source_collision)
                    if (
                        item["request_hash"] == request_digest
                        and item["account_id"] == command.actor.account_id
                        and item["child_id"] == command.child_id
                        and item["caregiver_id"] == command.actor.caregiver_id
                    ):
                        return {"decision": "replay", "operation": item}
                    return {"decision": "conflict", "code": "SOURCE_VERSION_CONFLICT"}

            # A new invalid command must not leave an operation or snapshot behind.
            # Existing idempotent/source replays were returned above first.
            ensure_action_allowed(ModuleState(state["module_state"]), command.action)

            connection.execute(
                """
                INSERT INTO nursery_operations (
                    operation_id, account_id, child_id, caregiver_id,
                    idempotency_key, request_hash, action, payload_json,
                    expected_state_version, status, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?)
                """,
                (
                    command.operation_id,
                    command.actor.account_id,
                    command.child_id,
                    command.actor.caregiver_id,
                    command.idempotency_key,
                    request_digest,
                    command.action.value,
                    canonical_json(command.payload),
                    command.expected_state_version,
                    stamp,
                    stamp,
                ),
            )
            if command.source:
                connection.execute(
                    """
                    INSERT INTO nursery_source_events (
                        account_id, child_id, operation_id, source_type,
                        source_id, source_version, specification_ref,
                        processing_status, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, 'accepted', ?, ?)
                    """,
                    (
                        command.actor.account_id,
                        command.child_id,
                        command.operation_id,
                        command.source.source_type.value,
                        command.source.source_id,
                        command.source.source_version,
                        command.source.specification_ref,
                        stamp,
                        stamp,
                    ),
                )
            row = connection.execute(
                "SELECT * FROM nursery_operations WHERE operation_id=?",
                (command.operation_id,),
            ).fetchone()
        return {"decision": "new", "operation": self._decode_operation(row)}

    @staticmethod
    def _snapshot_payload(state: sqlite3.Row | dict[str, Any]) -> dict[str, Any]:
        return {
            "module_state": str(state["module_state"]),
            "state_version": int(state["state_version"]),
            "stage_id": str(state["stage_id"]),
            "active_pause_count": int(state["active_pause_count"]),
        }

    @staticmethod
    def _creation_snapshot(
        connection: sqlite3.Connection, child_id: str
    ) -> dict[str, Any] | None:
        draft = connection.execute(
            """
            SELECT draft_version, child_kind, sex_status, stage_id,
                   selected_candidate_id, official_name, nickname,
                   address_terms_json, initial_space_json,
                   initial_space_completed, draft_status
            FROM nursery_creation_drafts WHERE child_id=?
            """,
            (str(child_id),),
        ).fetchone()
        if not draft:
            return None
        candidates = connection.execute(
            """
            SELECT candidate_id, caregiver_id, ordinal, proposed_name,
                   meaning_text, sound_notes, avoid_notes
            FROM nursery_name_candidates WHERE child_id=?
            ORDER BY caregiver_id, ordinal
            """,
            (str(child_id),),
        ).fetchall()
        preferences = connection.execute(
            """
            SELECT reviewer_id, candidate_id, preference
            FROM nursery_name_preferences WHERE child_id=?
            ORDER BY reviewer_id, candidate_id
            """,
            (str(child_id),),
        ).fetchall()
        submissions = connection.execute(
            """
            SELECT caregiver_id, questionnaire_kind, questionnaire_version,
                   answer_digest, submitted_at
            FROM nursery_private_submissions WHERE child_id=?
            ORDER BY caregiver_id, questionnaire_kind
            """,
            (str(child_id),),
        ).fetchall()
        temperament = connection.execute(
            """
            SELECT formula_version, summary_text, calculated_at, sealed_at
            FROM nursery_temperament_profiles WHERE child_id=?
            """,
            (str(child_id),),
        ).fetchone()
        result = dict(draft)
        result["address_terms"] = json.loads(result.pop("address_terms_json"))
        result["initial_space"] = json.loads(result.pop("initial_space_json"))
        result["initial_space_completed"] = bool(result["initial_space_completed"])
        result["name_candidates"] = [dict(row) for row in candidates]
        result["name_preferences"] = [dict(row) for row in preferences]
        result["questionnaire_submissions"] = [dict(row) for row in submissions]
        result["temperament"] = dict(temperament) if temperament else None
        return result

    def claim_operation(
        self,
        operation_id: str,
        *,
        lease_owner: str,
        lease_seconds: float,
    ) -> dict[str, Any]:
        now = self.clock()
        stamp = iso_time(now)
        expires = iso_time(now + timedelta(seconds=float(lease_seconds)))
        with self._write() as connection:
            operation = connection.execute(
                "SELECT * FROM nursery_operations WHERE operation_id=?",
                (operation_id,),
            ).fetchone()
            if not operation:
                raise NurseryError("OPERATION_NOT_FOUND", "operation was not found")
            status = OperationStatus(operation["status"])
            if status != OperationStatus.PENDING:
                return {
                    "decision": "not_claimed",
                    "operation": self._decode_operation(operation),
                }
            earlier = connection.execute(
                """
                SELECT operation_id, status FROM nursery_operations
                WHERE child_id=? AND sequence_no<?
                  AND status IN ('pending', 'processing', 'failed_retryable')
                ORDER BY sequence_no LIMIT 1
                """,
                (operation["child_id"], operation["sequence_no"]),
            ).fetchone()
            if earlier:
                return {
                    "decision": "queued",
                    "blocked_by": earlier["operation_id"],
                    "operation": self._decode_operation(operation),
                }

            state = connection.execute(
                """
                SELECT m.module_state, m.state_version, c.stage_id,
                       (SELECT COUNT(*) FROM nursery_pause_markers p
                        WHERE p.child_id=m.child_id AND p.released_at IS NULL)
                        AS active_pause_count
                FROM nursery_modules m
                JOIN nursery_children c ON c.child_id=m.child_id
                WHERE m.account_id=? AND m.child_id=?
                """,
                (operation["account_id"], operation["child_id"]),
            ).fetchone()
            # A second caregiver can arrive with a stale page version after the
            # first actual care was committed.  It is not a state conflict: the
            # same immutable plan version has already been carried out.  Finish
            # this operation as a truthful read-back before version rejection.
            if operation["action"] == NurseryAction.EXECUTE_CONFIRMED_CARE.value:
                try:
                    care_payload = json.loads(operation["payload_json"] or "{}")
                    care_plan_id = " ".join(
                        str(care_payload.get("care_plan_id") or "").split()
                    )
                    health_event_id = " ".join(
                        str(care_payload.get("health_event_id") or "").split()
                    )
                    care_plan_version = int(care_payload.get("care_plan_version") or 0)
                except (TypeError, ValueError):
                    care_plan_id = health_event_id = ""
                    care_plan_version = 0
                if care_plan_id and health_event_id and care_plan_version > 0:
                    execution = connection.execute(
                        """
                        SELECT e.execution_id, e.executed_at, e.caregiver_id,
                               p.display_name
                        FROM nursery_care_executions e
                        LEFT JOIN nursery_caregiver_profiles p
                          ON p.caregiver_id=e.caregiver_id AND p.child_id=e.child_id
                        WHERE e.child_id=? AND e.care_plan_id=?
                          AND e.care_plan_version=? AND e.health_event_id=?
                        """,
                        (
                            operation["child_id"],
                            care_plan_id,
                            care_plan_version,
                            health_event_id,
                        ),
                    ).fetchone()
                    if execution:
                        result = {
                            "action": NurseryAction.EXECUTE_CONFIRMED_CARE.value,
                            "changed": False,
                            "module_state": str(state["module_state"]),
                            "state_version": int(state["state_version"]),
                            "care_plan_id": care_plan_id,
                            "care_plan_version": care_plan_version,
                            "health_event_id": health_event_id,
                            "already_recorded": True,
                            "execution_id": str(execution["execution_id"]),
                            "executed_at": str(execution["executed_at"]),
                            "executed_by": str(execution["caregiver_id"]),
                            "executed_by_display_name": execution["display_name"],
                        }
                        connection.execute(
                            """
                            UPDATE nursery_operations SET status='completed',
                                result_json=?, error_code='', error_message='',
                                updated_at=?, completed_at=?
                            WHERE operation_id=?
                            """,
                            (canonical_json(result), stamp, stamp, operation_id),
                        )
                        connection.execute(
                            """
                            UPDATE nursery_source_events SET processing_status='applied',
                                updated_at=? WHERE operation_id=?
                            """,
                            (stamp, operation_id),
                        )
                        completed = connection.execute(
                            "SELECT * FROM nursery_operations WHERE operation_id=?",
                            (operation_id,),
                        ).fetchone()
                        return {
                            "decision": "completed",
                            "operation": self._decode_operation(completed),
                        }
            expected = operation["expected_state_version"]
            if expected is not None and int(expected) != int(state["state_version"]):
                connection.execute(
                    """
                    UPDATE nursery_operations SET status='rejected',
                        error_code='STATE_VERSION_CONFLICT',
                        error_message='expected_state_version does not match',
                        updated_at=?, completed_at=? WHERE operation_id=?
                    """,
                    (stamp, stamp, operation_id),
                )
                connection.execute(
                    "UPDATE nursery_source_events SET processing_status='rejected', "
                    "updated_at=? WHERE operation_id=?",
                    (stamp, operation_id),
                )
                rejected = connection.execute(
                    "SELECT * FROM nursery_operations WHERE operation_id=?",
                    (operation_id,),
                ).fetchone()
                return {
                    "decision": "rejected",
                    "operation": self._decode_operation(rejected),
                }

            before_payload = self._snapshot_payload(state)
            if operation["action"] == NurseryAction.APPLY_ANIMA_FAMILY_EVENT.value:
                runtime_row = connection.execute(
                    "SELECT state_json FROM nursery_child_runtime_state WHERE child_id=?",
                    (operation["child_id"],),
                ).fetchone()
                before_payload["runtime_state"] = self._runtime_from_row(
                    runtime_row["state_json"] if runtime_row else None
                )
            creation_before = self._creation_snapshot(
                connection, operation["child_id"]
            )
            if creation_before is not None:
                before_payload["creation_draft"] = creation_before
            connection.execute(
                """
                UPDATE nursery_operations SET status='processing',
                    before_state_version=?, lease_owner=?, lease_expires_at=?,
                    started_at=COALESCE(started_at, ?), updated_at=?
                WHERE operation_id=? AND status='pending'
                """,
                (
                    int(state["state_version"]),
                    lease_owner,
                    expires,
                    stamp,
                    stamp,
                    operation_id,
                ),
            )
            connection.execute(
                """
                INSERT OR IGNORE INTO nursery_state_snapshots (
                    operation_id, snapshot_kind, state_version, state_json, created_at
                ) VALUES (?, 'before', ?, ?, ?)
                """,
                (
                    operation_id,
                    int(state["state_version"]),
                    canonical_json(before_payload),
                    stamp,
                ),
            )
            claimed = connection.execute(
                "SELECT * FROM nursery_operations WHERE operation_id=?",
                (operation_id,),
            ).fetchone()
        return {
            "decision": "claimed",
            "operation": self._decode_operation(claimed),
            "before": before_payload,
        }

    def prepare_explicit_resume(self, operation_id: str) -> dict[str, Any]:
        now = self.clock()
        stamp = iso_time(now)
        with self._write() as connection:
            row = connection.execute(
                "SELECT * FROM nursery_operations WHERE operation_id=?",
                (operation_id,),
            ).fetchone()
            if not row:
                raise NurseryError("OPERATION_NOT_FOUND", "operation was not found")
            status = OperationStatus(row["status"])
            if status == OperationStatus.PROCESSING:
                expires = parse_time(row["lease_expires_at"])
                if expires is None or expires > now:
                    return {"decision": "busy", "operation": self._decode_operation(row)}
                connection.execute(
                    """
                    UPDATE nursery_operations SET status='failed_retryable',
                        error_code='STALE_PROCESSING_LEASE',
                        error_message='processing lease expired', lease_owner=NULL,
                        lease_expires_at=NULL, updated_at=? WHERE operation_id=?
                    """,
                    (stamp, operation_id),
                )
                status = OperationStatus.FAILED_RETRYABLE
            if status == OperationStatus.FAILED_RETRYABLE:
                connection.execute(
                    """
                    UPDATE nursery_operations SET status='pending', error_code='',
                        error_message='', lease_owner=NULL, lease_expires_at=NULL,
                        updated_at=? WHERE operation_id=?
                    """,
                    (stamp, operation_id),
                )
                row = connection.execute(
                    "SELECT * FROM nursery_operations WHERE operation_id=?",
                    (operation_id,),
                ).fetchone()
                return {"decision": "resumed", "operation": self._decode_operation(row)}
            return {"decision": "unchanged", "operation": self._decode_operation(row)}

    def _confirmation_complete(
        self,
        connection: sqlite3.Connection,
        *,
        subject_id: str,
        subject_version: str,
    ) -> bool:
        rows = connection.execute(
            """
            SELECT DISTINCT c.role
            FROM nursery_confirmations n
            JOIN nursery_caregivers c ON c.caregiver_id=n.caregiver_id
            WHERE n.subject_type='child_deletion' AND n.subject_id=?
              AND n.subject_version=? AND n.confirmation_status='confirmed'
              AND c.permission_status='active'
            """,
            (subject_id, subject_version),
        ).fetchall()
        roles = {str(row["role"]) for row in rows}
        return {
            CaregiverRole.USER_GUARDIAN.value,
            CaregiverRole.EXTERNAL_AI_GUARDIAN.value,
        }.issubset(roles)

    @staticmethod
    def _bump_draft_version(
        connection: sqlite3.Connection, child_id: str, stamp: str
    ) -> int:
        row = connection.execute(
            "SELECT draft_version FROM nursery_creation_drafts WHERE child_id=?",
            (child_id,),
        ).fetchone()
        if not row:
            raise NurseryError("DRAFT_NOT_FOUND", "creation draft was not found")
        version = int(row["draft_version"]) + 1
        connection.execute(
            """
            UPDATE nursery_creation_drafts
            SET draft_version=?, updated_at=? WHERE child_id=?
            """,
            (version, stamp, child_id),
        )
        return version

    @staticmethod
    def _shared_name_candidate_ids(
        connection: sqlite3.Connection, child_id: str
    ) -> list[str]:
        """Return candidates accepted by both active founding-guardian roles."""

        rows = connection.execute(
            """
            SELECT n.candidate_id
            FROM nursery_name_preferences n
            JOIN nursery_caregivers c ON c.caregiver_id=n.reviewer_id
            JOIN nursery_caregiver_profiles p ON p.caregiver_id=c.caregiver_id
            WHERE n.child_id=? AND n.preference IN ('like', 'acceptable')
              AND c.permission_status='active' AND p.founding_guardian=1
              AND c.role IN ('user_guardian', 'external_ai_guardian')
            GROUP BY n.candidate_id
            HAVING COUNT(DISTINCT c.role)=2
            ORDER BY n.candidate_id
            """,
            (child_id,),
        ).fetchall()
        return [str(row["candidate_id"]) for row in rows]

    @classmethod
    def _settle_shared_name(
        cls, connection: sqlite3.Connection, child_id: str, stamp: str
    ) -> str | None:
        """Auto-select the sole shared candidate; never guess between several."""

        shared = cls._shared_name_candidate_ids(connection, child_id)
        if len(shared) == 1:
            candidate = connection.execute(
                """
                SELECT proposed_name FROM nursery_name_candidates
                WHERE child_id=? AND candidate_id=?
                """,
                (child_id, shared[0]),
            ).fetchone()
            if not candidate:
                raise NurseryError("NAME_CANDIDATE_NOT_FOUND", "name candidate not found")
            connection.execute(
                """
                UPDATE nursery_creation_drafts
                SET selected_candidate_id=?, official_name=?, updated_at=?
                WHERE child_id=?
                """,
                (shared[0], candidate["proposed_name"], stamp, child_id),
            )
            return str(candidate["proposed_name"])
        selected = connection.execute(
            """
            SELECT selected_candidate_id FROM nursery_creation_drafts
            WHERE child_id=?
            """,
            (child_id,),
        ).fetchone()
        if not selected or str(selected["selected_candidate_id"] or "") not in shared:
            connection.execute(
                """
                UPDATE nursery_creation_drafts
                SET selected_candidate_id=NULL, official_name=NULL, updated_at=?
                WHERE child_id=?
                """,
                (stamp, child_id),
            )
        return None

    @staticmethod
    def _creation_confirmation_complete(
        connection: sqlite3.Connection,
        *,
        child_id: str,
        subject_version: str,
    ) -> bool:
        rows = connection.execute(
            """
            SELECT DISTINCT c.role
            FROM nursery_confirmations n
            JOIN nursery_caregivers c ON c.caregiver_id=n.caregiver_id
            JOIN nursery_caregiver_profiles p ON p.caregiver_id=c.caregiver_id
            WHERE n.subject_type='child_creation' AND n.subject_id=?
              AND n.subject_version=? AND n.confirmation_status='confirmed'
              AND c.permission_status='active' AND p.founding_guardian=1
            """,
            (child_id, subject_version),
        ).fetchall()
        roles = {str(row["role"]) for row in rows}
        return {
            CaregiverRole.USER_GUARDIAN.value,
            CaregiverRole.EXTERNAL_AI_GUARDIAN.value,
        }.issubset(roles)

    @staticmethod
    def _creation_readiness(
        connection: sqlite3.Connection,
        *,
        account_id: str,
        child_id: str,
    ) -> list[str]:
        missing: list[str] = []
        draft = connection.execute(
            "SELECT * FROM nursery_creation_drafts WHERE child_id=?",
            (child_id,),
        ).fetchone()
        if not draft or draft["draft_status"] != "open":
            return ["open_draft"]
        roles = {
            str(row["role"])
            for row in connection.execute(
                """
                SELECT c.role FROM nursery_caregivers c
                JOIN nursery_caregiver_profiles p
                  ON p.caregiver_id=c.caregiver_id
                WHERE c.account_id=? AND c.permission_status='active'
                  AND p.child_id=? AND p.founding_guardian=1
                """,
                (account_id, child_id),
            ).fetchall()
        }
        required_roles = {
            CaregiverRole.USER_GUARDIAN.value,
            CaregiverRole.EXTERNAL_AI_GUARDIAN.value,
        }
        if not required_roles.issubset(roles):
            missing.append("two_founding_guardians")
        model = connection.execute(
            """
            SELECT connection_status FROM nursery_model_configs WHERE account_id=?
            """,
            (account_id,),
        ).fetchone()
        if not model or model["connection_status"] != "ready":
            missing.append("model_connection")
        if not draft["official_name"] or not draft["selected_candidate_id"]:
            missing.append("agreed_name")
        else:
            accepted = connection.execute(
                """
                SELECT DISTINCT c.role FROM nursery_name_preferences n
                JOIN nursery_caregivers c ON c.caregiver_id=n.reviewer_id
                JOIN nursery_caregiver_profiles p ON p.caregiver_id=c.caregiver_id
                WHERE n.child_id=? AND n.candidate_id=?
                  AND n.preference IN ('like', 'acceptable')
                  AND c.permission_status='active' AND p.founding_guardian=1
                """,
                (child_id, draft["selected_candidate_id"]),
            ).fetchall()
            if not required_roles.issubset({str(row["role"]) for row in accepted}):
                missing.append("name_consensus")
        for kind, label in (
            (QuestionnaireKind.TEMPERAMENT.value, "temperament_questionnaires"),
            (QuestionnaireKind.INITIAL_STYLE.value, "initial_style_questionnaires"),
        ):
            submitted = connection.execute(
                """
                SELECT DISTINCT c.role FROM nursery_private_submissions s
                JOIN nursery_caregivers c ON c.caregiver_id=s.caregiver_id
                JOIN nursery_caregiver_profiles p ON p.caregiver_id=c.caregiver_id
                WHERE s.child_id=? AND s.questionnaire_kind=?
                  AND c.permission_status='active' AND p.founding_guardian=1
                """,
                (child_id, kind),
            ).fetchall()
            if not required_roles.issubset({str(row["role"]) for row in submitted}):
                missing.append(label)
        temperament = connection.execute(
            "SELECT child_id FROM nursery_temperament_profiles WHERE child_id=?",
            (child_id,),
        ).fetchone()
        if not temperament:
            missing.append("temperament_result")
        if not int(draft["initial_space_completed"]):
            missing.append("initial_space")
        if draft["sex_status"] not in {"boy", "girl"}:
            missing.append("resolved_sex")
        if draft["stage_id"] not in {
            "infancy",
            "early_walker",
            "toddler",
            "young_child",
        }:
            missing.append("valid_stage")
        return missing

    def apply_claimed_operation(
        self,
        operation_id: str,
        *,
        lease_owner: str,
        prepared: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Short transaction B: recheck version and atomically commit the result."""

        now = self.clock()
        stamp = iso_time(now)
        with self._write() as connection:
            operation = connection.execute(
                "SELECT * FROM nursery_operations WHERE operation_id=?",
                (operation_id,),
            ).fetchone()
            if not operation:
                raise NurseryError("OPERATION_NOT_FOUND", "operation was not found")
            if (
                operation["status"] != OperationStatus.PROCESSING.value
                or operation["lease_owner"] != lease_owner
            ):
                raise NurseryError("LEASE_LOST", "operation processing lease is not owned")
            lease_expires = parse_time(operation["lease_expires_at"])
            if lease_expires is None or lease_expires <= now:
                raise NurseryError("LEASE_EXPIRED", "operation processing lease expired")

            state = connection.execute(
                """
                SELECT m.module_state, m.state_version, m.pre_delete_state,
                       m.recycle_deadline, c.stage_id,
                       (SELECT COUNT(*) FROM nursery_pause_markers p
                        WHERE p.child_id=m.child_id AND p.released_at IS NULL)
                        AS active_pause_count
                FROM nursery_modules m
                JOIN nursery_children c ON c.child_id=m.child_id
                WHERE m.account_id=? AND m.child_id=?
                """,
                (operation["account_id"], operation["child_id"]),
            ).fetchone()
            before_version = int(operation["before_state_version"])
            if int(state["state_version"]) != before_version:
                connection.execute(
                    """
                    UPDATE nursery_operations SET status='failed_retryable',
                        error_code='STATE_VERSION_CHANGED',
                        error_message='state changed after before snapshot',
                        lease_owner=NULL, lease_expires_at=NULL, updated_at=?
                    WHERE operation_id=?
                    """,
                    (stamp, operation_id),
                )
                failed = connection.execute(
                    "SELECT * FROM nursery_operations WHERE operation_id=?",
                    (operation_id,),
                ).fetchone()
                return self._decode_operation(failed)

            action = NurseryAction(operation["action"])
            current = ModuleState(state["module_state"])
            payload = json.loads(operation["payload_json"] or "{}")
            prepared = dict(prepared or {})
            active_pause_count = int(state["active_pause_count"])
            deletion_confirmed = False
            creation_confirmed = False
            mutation_changed = False
            body_settlement_changed = False
            draft_version: int | None = None
            result_extra: dict[str, Any] = {}

            def required_text(name: str, *, maximum: int = 500) -> str:
                value = " ".join(str(payload.get(name) or "").split())
                if not value or len(value) > maximum:
                    raise NurseryError("INVALID_FEATURE_INPUT", f"{name} is required")
                return value

            def optional_text(name: str, *, maximum: int = 1000) -> str | None:
                value = " ".join(str(payload.get(name) or "").split())
                if len(value) > maximum:
                    raise NurseryError("INVALID_FEATURE_INPUT", f"{name} is too long")
                return value or None

            def latest_proposal(proposal_type: str) -> sqlite3.Row | None:
                return connection.execute(
                    """
                    SELECT * FROM nursery_proposals
                    WHERE child_id=? AND proposal_type=?
                    ORDER BY proposal_version DESC, updated_at DESC LIMIT 1
                    """,
                    (operation["child_id"], proposal_type),
                ).fetchone()

            def create_proposal(proposal_type: str, proposed: dict[str, Any]) -> tuple[str, int]:
                current_proposal = latest_proposal(proposal_type)
                version = int(current_proposal["proposal_version"] if current_proposal else 0) + 1
                if current_proposal and current_proposal["status"] == "open":
                    connection.execute(
                        "UPDATE nursery_proposals SET status='withdrawn', updated_at=? WHERE proposal_id=?",
                        (stamp, current_proposal["proposal_id"]),
                    )
                proposal_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"nursery:{proposal_type}:{operation_id}"))
                connection.execute(
                    """
                    INSERT INTO nursery_proposals (
                        proposal_id, child_id, proposal_type, proposal_version,
                        proposed_json, status, proposed_by, source_operation_id,
                        created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, 'open', ?, ?, ?, ?)
                    """,
                    (
                        proposal_id, operation["child_id"], proposal_type, version,
                        canonical_json(proposed), operation["caregiver_id"],
                        operation_id, stamp, stamp,
                    ),
                )
                return proposal_id, version

            def commit_body_settlement(
                settlement: Any,
                *,
                runtime_state: dict[str, Any] | None = None,
                care_action: Any = None,
            ) -> dict[str, dict[str, Any]]:
                """Consume one previously projected cursor inside this write."""

                nonlocal body_settlement_changed

                if not isinstance(settlement, dict) or not isinstance(
                    settlement.get("clock"), dict
                ):
                    raise NurseryError(
                        "INVALID_BODY_SETTLEMENT", "body settlement is invalid"
                    )
                body_clock = dict(settlement["clock"])
                if str(body_clock.get("child_id") or "") != str(operation["child_id"]):
                    raise NurseryError(
                        "INVALID_BODY_SETTLEMENT", "body settlement child is invalid"
                    )
                expected_settled = settlement.get("expected_settled_through")
                expected_runtime = settlement.get("expected_runtime_state")
                projected_runtime = settlement.get("runtime_state")
                if (
                    not expected_settled
                    or not isinstance(expected_runtime, dict)
                    or not isinstance(projected_runtime, dict)
                ):
                    raise NurseryError(
                        "INVALID_BODY_SETTLEMENT", "body settlement anchor is missing"
                    )
                current_clock = connection.execute(
                    """
                    SELECT c.settled_through, r.state_json
                    FROM nursery_body_clocks c
                    LEFT JOIN nursery_child_runtime_state r ON r.child_id=c.child_id
                    WHERE c.child_id=?
                    """,
                    (operation["child_id"],),
                ).fetchone()
                current_settled = (
                    str(current_clock["settled_through"])
                    if current_clock
                    else str(expected_settled)
                )
                if current_settled != str(expected_settled):
                    raise NurseryError(
                        "BODY_STATE_CHANGED",
                        "body state changed after the write was prepared",
                    )
                current_runtime = self._runtime_from_row(
                    current_clock["state_json"]
                    if current_clock and current_clock["state_json"]
                    else expected_runtime
                )
                if canonical_json(current_runtime) != canonical_json(expected_runtime):
                    raise NurseryError(
                        "BODY_STATE_CHANGED",
                        "body state changed after the write was prepared",
                    )
                saved_runtime = runtime_state if runtime_state is not None else projected_runtime
                if not isinstance(saved_runtime, dict):
                    raise NurseryError("INVALID_RUNTIME_STATE", "child state is invalid")
                saved_runtime = dict(saved_runtime)
                if runtime_state is not None:
                    for field in ("thirst", "hunger", "fatigue", "unwell"):
                        if float(saved_runtime.get(field, 0.0)) != float(
                            projected_runtime.get(field, 0.0)
                        ):
                            raise NurseryError(
                                "INVALID_RUNTIME_STATE",
                                "interaction cannot change body or health state",
                            )
                state_before_care = dict(saved_runtime)
                clock_before_care = dict(body_clock)
                normalized_care = normalize_care_action(care_action)
                saved_runtime, clock_updates = apply_completed_care(
                    saved_runtime, normalized_care, stamp=stamp
                )
                body_clock.update(clock_updates)
                body_settlement_changed = (
                    canonical_json(current_runtime) != canonical_json(saved_runtime)
                    or current_settled != str(body_clock.get("settled_through"))
                )
                connection.execute(
                    """
                    INSERT INTO nursery_child_runtime_state (
                        child_id, state_json, state_updated_at
                    ) VALUES (?, ?, ?)
                    ON CONFLICT(child_id) DO UPDATE SET
                        state_json=excluded.state_json,
                        state_updated_at=excluded.state_updated_at
                    """,
                    (operation["child_id"], canonical_json(saved_runtime), stamp),
                )
                self._save_body_clock(connection, body_clock)
                return {
                    "state_before": state_before_care,
                    "state_after": dict(saved_runtime),
                    "clock_before": clock_before_care,
                }

            if action not in {NurseryAction.CHILD_INTERACT, NurseryAction.CORRECT_CARE_EVENT} and "body_settlement" in prepared:
                commit_body_settlement(prepared["body_settlement"])

            if action == NurseryAction.CHILD_INTERACT:
                required = {
                    "event_id",
                    "runtime_state",
                    "caregiver_message",
                    "child_reply",
                    "intent",
                    "conversation_channel",
                    "care_action",
                    "expires_at",
                    "body_settlement",
                }
                if not required.issubset(prepared):
                    raise NurseryError(
                        "INTERACTION_PREPARATION_REQUIRED",
                        "a validated child interaction proposal is required",
                    )
                runtime_state = prepared["runtime_state"]
                if not isinstance(runtime_state, dict):
                    raise NurseryError("INVALID_RUNTIME_STATE", "child state is invalid")
                message = str(prepared["caregiver_message"]).strip()
                reply = str(prepared["child_reply"]).strip()
                intent = str(prepared["intent"]).strip()
                if not message or len(message) > 1200 or not reply or len(reply) > 500:
                    raise NurseryError("INVALID_CHILD_INTERACTION", "child interaction text is invalid")
                if not intent or len(intent) > 40:
                    raise NurseryError("INVALID_CHILD_INTERACTION", "child interaction intent is invalid")
                channel = str(prepared["conversation_channel"] or "").strip()
                if channel not in {"user_child", "external_ai_child", "shared_family"}:
                    raise NurseryError("INVALID_CHILD_INTERACTION", "child interaction channel is invalid")
                care_action = normalize_care_action(prepared["care_action"])
                care_commit = commit_body_settlement(
                    prepared["body_settlement"],
                    runtime_state=runtime_state,
                    care_action=care_action,
                )
                connection.execute(
                    """
                    INSERT INTO nursery_child_short_events (
                        event_id, child_id, operation_id, caregiver_id, event_kind,
                        conversation_channel, caregiver_message, child_reply, intent,
                        created_at, expires_at
                    ) VALUES (?, ?, ?, ?, 'interaction', ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        str(prepared["event_id"]),
                        operation["child_id"],
                        operation_id,
                        operation["caregiver_id"],
                        channel,
                        message,
                        reply,
                        intent,
                        stamp,
                        str(prepared["expires_at"]),
                    ),
                )
                if care_action["status"] == "completed":
                    care_event_id = f"care:{operation_id}"
                    world_item_id = (
                        str(
                            uuid.uuid5(
                                uuid.NAMESPACE_URL,
                                f"nursery:conversation-item:{operation_id}",
                            )
                        )
                        if care_action["kind"] in {"gift", "world_item"}
                        else None
                    )
                    connection.execute(
                        """
                        INSERT INTO nursery_shared_care_events (
                            care_event_id, child_id, source_operation_id, caregiver_id,
                            conversation_channel, category, object_name, summary,
                            world_item_id, state_before_json, state_after_json, created_at, clock_before_json
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            care_event_id,
                            operation["child_id"],
                            operation_id,
                            operation["caregiver_id"],
                            channel,
                            care_action["kind"],
                            care_action["object_name"],
                            care_action["summary"],
                            world_item_id,
                            canonical_json(care_commit["state_before"]),
                            canonical_json(care_commit["state_after"]),
                            stamp,
                            canonical_json(care_commit["clock_before"]),
                        ),
                    )
                    result_extra["care"] = {
                        "care_event_id": care_event_id,
                        "category": care_action["kind"],
                        "object_name": care_action["object_name"],
                        "summary": care_action["summary"],
                    }
                    if world_item_id:
                        connection.execute(
                            """
                            INSERT INTO nursery_items (
                                item_id, child_id, area_id, name, emoji, description,
                                current_state, facts_json, source_caregiver_id,
                                last_interacted_at, removed_at, created_at, updated_at
                            ) VALUES (?, ?, NULL, ?, '', ?, '正在使用', ?, ?, ?, NULL, ?, ?)
                            """,
                            (
                                world_item_id,
                                operation["child_id"],
                                care_action["object_name"],
                                care_action["summary"],
                                canonical_json({"source": "child_interaction", "care_event_id": care_event_id}),
                                operation["caregiver_id"],
                                stamp,
                                stamp,
                                stamp,
                            ),
                        )
                        result_extra["care"]["world_item_id"] = world_item_id
                mutation_changed = True
                result_extra["child_event_id"] = str(prepared["event_id"])
            elif action == NurseryAction.ADD_AREA:
                category = str(payload.get("category") or "custom").strip().lower()
                if category not in {"room", "wall", "floor", "window", "sleep", "bedding", "lighting", "storage", "furniture", "custom"}:
                    raise NurseryError("INVALID_AREA_CATEGORY", "area category is invalid")
                room = connection.execute("SELECT room_id FROM nursery_rooms WHERE child_id=?", (operation["child_id"],)).fetchone()
                if not room:
                    room_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"nursery:room:{operation['child_id']}"))
                    connection.execute(
                        "INSERT INTO nursery_rooms (room_id, child_id, name, atmosphere, facts_json, created_at, updated_at) VALUES (?, ?, ?, NULL, '{}', ?, ?)",
                        (room_id, operation["child_id"], "孩子的房间", stamp, stamp),
                    )
                else:
                    room_id = str(room["room_id"])
                area_id = str(payload.get("area_id") or "").strip() or str(uuid.uuid5(uuid.NAMESPACE_URL, f"nursery:area:{operation_id}"))
                connection.execute(
                    """
                    INSERT INTO nursery_areas (area_id, child_id, room_id, category, label, description, facts_json, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (area_id, operation["child_id"], room_id, category, required_text("label", maximum=120), optional_text("description", maximum=1000), canonical_json(dict(payload.get("facts") or {})), stamp, stamp),
                )
                mutation_changed = True
                result_extra["area_id"] = area_id
            elif action == NurseryAction.UPDATE_AREA:
                area_id = required_text("area_id", maximum=200)
                row = connection.execute("SELECT * FROM nursery_areas WHERE area_id=? AND child_id=?", (area_id, operation["child_id"])).fetchone()
                if not row:
                    raise NurseryError("AREA_NOT_FOUND", "area was not found")
                label = optional_text("label", maximum=120) or str(row["label"])
                description = optional_text("description", maximum=1000) if "description" in payload else row["description"]
                connection.execute("UPDATE nursery_areas SET label=?, description=?, updated_at=? WHERE area_id=?", (label, description, stamp, area_id))
                mutation_changed = True
                result_extra["area_id"] = area_id
            elif action == NurseryAction.ADD_ITEM:
                item_facts = payload.get("facts") or {}
                if not isinstance(item_facts, dict) or {"recorded_by", "activity_id"} & set(item_facts):
                    raise NurseryError("INVALID_ITEM_FACTS", "作品来源由已认证的操作生成，不能手工指定。")
                artwork = item_facts.get("artwork_data_url")
                if artwork is not None:
                    if not isinstance(artwork, str) or not artwork.startswith(
                        ("data:image/png;base64,", "data:image/webp;base64,", "data:image/jpeg;base64,")
                    ):
                        raise NurseryError("INVALID_ARTWORK", "作品图片必须是 PNG、WebP 或 JPEG。")
                    try:
                        raw_artwork = base64.b64decode(artwork.split(",", 1)[1], validate=True)
                    except (ValueError, base64.binascii.Error):
                        raise NurseryError("INVALID_ARTWORK", "作品图片内容无效。")
                    if not raw_artwork or len(raw_artwork) > 400_000:
                        raise NurseryError("ARTWORK_TOO_LARGE", "作品图片压缩后不能超过 400KB。")
                    item_facts = {"artwork_data_url": artwork, "artwork_kind": "drawing"}
                area_id = str(payload.get("area_id") or "").strip() or None
                if area_id and not connection.execute("SELECT 1 FROM nursery_areas WHERE area_id=? AND child_id=?", (area_id, operation["child_id"])).fetchone():
                    raise NurseryError("AREA_NOT_FOUND", "target area was not found")
                item_id = str(payload.get("item_id") or "").strip() or str(uuid.uuid5(uuid.NAMESPACE_URL, f"nursery:item:{operation_id}"))
                connection.execute(
                    """
                    INSERT INTO nursery_items (item_id, child_id, area_id, name, emoji, description, current_state, facts_json, source_caregiver_id, last_interacted_at, removed_at, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, ?)
                    """,
                    (item_id, operation["child_id"], area_id, required_text("name", maximum=120), optional_text("emoji", maximum=16), optional_text("description", maximum=1000), optional_text("current_state", maximum=240), canonical_json(item_facts), operation["caregiver_id"], stamp, stamp, stamp),
                )
                mutation_changed = True
                result_extra["item_id"] = item_id
            elif action in {NurseryAction.MOVE_ITEM, NurseryAction.UPDATE_ITEM, NurseryAction.STORE_ITEM, NurseryAction.REMOVE_ITEM}:
                item_id = required_text("item_id", maximum=200)
                item = connection.execute("SELECT * FROM nursery_items WHERE item_id=? AND child_id=? AND removed_at IS NULL", (item_id, operation["child_id"])).fetchone()
                if not item:
                    raise NurseryError("ITEM_NOT_FOUND", "item was not found")
                if action == NurseryAction.REMOVE_ITEM and payload.get("confirmed") is not True:
                    raise NurseryError("CONFIRMATION_REQUIRED", "removing an item requires explicit confirmation")
                target_area = item["area_id"]
                removed_at = item["removed_at"]
                name = str(item["name"])
                description = item["description"]
                current_state = item["current_state"]
                if action == NurseryAction.MOVE_ITEM:
                    target_area = str(payload.get("target_area_id") or "").strip()
                    if not target_area or not connection.execute("SELECT 1 FROM nursery_areas WHERE area_id=? AND child_id=?", (target_area, operation["child_id"])).fetchone():
                        raise NurseryError("AREA_NOT_FOUND", "target area was not found")
                elif action == NurseryAction.UPDATE_ITEM:
                    name = optional_text("name", maximum=120) or name
                    description = optional_text("description", maximum=1000) if "description" in payload else description
                    current_state = optional_text("current_state", maximum=240) if "current_state" in payload else current_state
                elif action == NurseryAction.STORE_ITEM:
                    current_state = optional_text("current_state", maximum=240) or "stored"
                else:
                    removed_at = stamp
                connection.execute(
                    "UPDATE nursery_items SET area_id=?, name=?, description=?, current_state=?, last_interacted_at=?, removed_at=?, updated_at=? WHERE item_id=?",
                    (target_area, name, description, current_state, stamp, removed_at, stamp, item_id),
                )
                mutation_changed = True
                result_extra["item_id"] = item_id
            elif action == NurseryAction.SET_REST_STATE:
                if type(payload.get("sleeping")) is not bool or set(payload) - {"sleeping", "summary"}:
                    raise NurseryError("INVALID_REST_STATE", "sleeping must be a boolean with a care summary")
                rest_summary = required_text("summary", maximum=800)
                row = connection.execute("SELECT * FROM nursery_body_clocks WHERE child_id=?", (operation["child_id"],)).fetchone()
                body_clock = dict(row) if row else self._body_clock_payload(operation["child_id"],stamp=stamp,rule_version="nursery-care-state-v2")
                before_rest = bool(body_clock["sleep_started_at"])
                if before_rest != payload["sleeping"]:
                    body_clock["sleep_started_at"] = stamp if payload["sleeping"] else None
                    if not payload["sleeping"]:
                        body_clock["last_woke_at"] = stamp
                    body_clock["settled_through"] = stamp
                    body_clock["updated_at"] = stamp
                    self._save_body_clock(connection, body_clock)
                    connection.execute(
                        """INSERT INTO nursery_family_events
                        (event_id,child_id,caregiver_id,kind,summary,before_json,after_json,source_operation_id,created_at)
                        VALUES (?,?,?,'rest',?,?,?,?,?)""",
                        (f"rest:{operation_id}", operation["child_id"], operation["caregiver_id"], rest_summary,
                         canonical_json({"sleeping":before_rest}),canonical_json({"sleeping":payload["sleeping"]}),operation_id,stamp),
                    )
                    mutation_changed = True
                result_extra["sleeping"] = payload["sleeping"]
            elif action == NurseryAction.CORRECT_CARE_EVENT:
                care_id = required_text("care_event_id", maximum=200)
                reason = required_text("reason", maximum=800)
                care = connection.execute("SELECT * FROM nursery_shared_care_events WHERE care_event_id=? AND child_id=?", (care_id, operation["child_id"])).fetchone()
                if not care or care["created_at"] <= iso_time(now - timedelta(days=30)):
                    raise NurseryError("CARE_EVENT_NOT_FOUND", "这条照顾记录已经不在保留范围内。")
                actor_role = connection.execute("SELECT role FROM nursery_caregivers WHERE caregiver_id=?", (operation["caregiver_id"],)).fetchone()
                if care["caregiver_id"] != operation["caregiver_id"] and actor_role["role"] != CaregiverRole.USER_GUARDIAN.value:
                    raise NurseryError("CARE_CORRECTION_OWNER_REQUIRED", "只能纠正自己记录的照顾；其他记录请与用户商量。")
                if care["correction_json"]:
                    result_extra.update({"care_event_id": care_id, "already_corrected": True})
                else:
                    if care["category"] == "health":
                        raise NurseryError("HEALTH_CORRECTION_REQUIRES_REVIEW", "健康照料不能直接撤销，请先补充当前观察并核对确认安排。")
                    original_before = self._json_value(care["state_before_json"], {})
                    original_after = self._json_value(care["state_after_json"], {})
                    clock_before = self._json_value(care["clock_before_json"], {})
                    current_row = connection.execute("SELECT state_json FROM nursery_child_runtime_state WHERE child_id=?", (operation["child_id"],)).fetchone()
                    current_runtime = self._runtime_from_row(current_row["state_json"] if current_row else None)
                    affected = [key for key in original_before if original_before[key] != original_after.get(key)]
                    if not clock_before:
                        raise NurseryError("CARE_CORRECTION_CONFLICT", "旧记录缺少可核对的状态快照，不能自动回滚。")
                    if care["world_item_id"]:
                        item = connection.execute("SELECT * FROM nursery_items WHERE item_id=? AND child_id=?", (care["world_item_id"], operation["child_id"])).fetchone()
                        if item and item["updated_at"] == care["created_at"]:
                            connection.execute("UPDATE nursery_items SET removed_at=?,updated_at=? WHERE item_id=? AND child_id=?", (stamp, stamp, care["world_item_id"], operation["child_id"]))
                    for key in affected:
                        before_value, after_value = original_before.get(key), original_after.get(key)
                        if type(before_value) in {int, float} and type(after_value) in {int, float} and type(current_runtime.get(key)) in {int, float}:
                            current_runtime[key] = round(max(0.0, min(1.0, float(current_runtime[key]) - (float(after_value) - float(before_value)))), 3)
                        elif current_runtime.get(key) == after_value:
                            current_runtime[key] = before_value
                    connection.execute("UPDATE nursery_child_runtime_state SET state_json=?,state_updated_at=? WHERE child_id=?", (canonical_json(current_runtime), stamp, operation["child_id"]))
                    correction = {"reason": reason, "caregiver_id": operation["caregiver_id"], "operation_id": operation_id, "created_at": stamp}
                    connection.execute("UPDATE nursery_shared_care_events SET correction_json=? WHERE care_event_id=?", (canonical_json(correction), care_id))
                    latest_care = connection.execute(
                        "SELECT MAX(created_at) AS value FROM nursery_shared_care_events WHERE child_id=? AND care_event_id<>? AND correction_json IS NULL",
                        (operation["child_id"], care_id),
                    ).fetchone()["value"]
                    latest_food = connection.execute(
                        "SELECT MAX(created_at) AS value FROM nursery_shared_care_events WHERE child_id=? AND care_event_id<>? AND correction_json IS NULL AND category='food'",
                        (operation["child_id"], care_id),
                    ).fetchone()["value"]
                    latest_drink = connection.execute(
                        "SELECT MAX(created_at) AS value FROM nursery_shared_care_events WHERE child_id=? AND care_event_id<>? AND correction_json IS NULL AND category='drink'",
                        (operation["child_id"], care_id),
                    ).fetchone()["value"]
                    connection.execute(
                        "UPDATE nursery_body_clocks SET last_care_at=?,last_fed_at=?,last_hydrated_at=?,updated_at=? WHERE child_id=?",
                        (latest_care or clock_before.get("last_care_at"),
                         latest_food or clock_before.get("last_fed_at"),
                         latest_drink or clock_before.get("last_hydrated_at"),
                         stamp, operation["child_id"]),
                    )
                    mutation_changed = True
                    result_extra.update({"care_event_id": care_id, "correction": correction, "recalculation": "event_delta_reversed"})
            elif action in {NurseryAction.UPDATE_FAMILY_THREAD, NurseryAction.RECORD_RELATIONSHIP_EVENT}:
                from .family_life import apply_family_action
                result_extra.update(apply_family_action(connection, operation, payload, stamp))
                mutation_changed = True
            elif action == NurseryAction.RECORD_GROWTH_OBSERVATION:
                observation = payload.get("observation") or {}
                if not isinstance(observation, dict):
                    raise NurseryError("INVALID_GROWTH_OBSERVATION", "observation must be an object")
                ability_ids = observation.get("ability_ids") or []
                if not isinstance(ability_ids, list) or not ability_ids or any(not str(item).strip() for item in ability_ids):
                    raise NurseryError("INVALID_GROWTH_OBSERVATION", "ability_ids are required")
                session_id = " ".join(str(observation.get("session_id") or "").split())
                behavior = " ".join(str(observation.get("observed_behavior") or "").split())
                assistance = " ".join(str(observation.get("assistance") or "").split())
                if not session_id or len(session_id) > 200 or not behavior or len(behavior) > 1000 or len(assistance) > 500:
                    raise NurseryError("INVALID_GROWTH_OBSERVATION", "growth observation is invalid")
                observation_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"nursery:growth:{operation_id}"))
                connection.execute(
                    """
                    INSERT INTO nursery_growth_observations (observation_id, child_id, caregiver_id, session_id, ability_ids_json, observed_behavior, assistance, source_operation_id, valid, observed_at, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?)
                    """,
                    (observation_id, operation["child_id"], operation["caregiver_id"], session_id, canonical_json([str(item).strip() for item in ability_ids]), behavior, assistance, operation_id, str(observation.get("observed_at") or stamp), stamp),
                )
                mutation_changed = True
                result_extra["observation_id"] = observation_id
            elif action == NurseryAction.PROPOSE_STAGE:
                target_stage = str(payload.get("target_stage") or "").strip()
                if target_stage not in {"infancy", "early_walker", "toddler", "young_child"} or target_stage == str(state["stage_id"]):
                    raise NurseryError("INVALID_TARGET_STAGE", "target stage is invalid")
                proposal_id, proposal_version = create_proposal("stage", {"target_stage": target_stage})
                mutation_changed = True
                result_extra.update({"proposal_id": proposal_id, "proposal_version": proposal_version})
            elif action == NurseryAction.CONFIRM_STAGE:
                proposal_id = required_text("proposal_id", maximum=200)
                proposal = connection.execute("SELECT * FROM nursery_proposals WHERE proposal_id=? AND child_id=? AND proposal_type='stage' AND status='open'", (proposal_id, operation["child_id"])).fetchone()
                if not proposal or int(payload.get("proposal_version") or 0) != int(proposal["proposal_version"]):
                    raise NurseryError("PROPOSAL_VERSION_CONFLICT", "stage proposal is not current")
                proposed = self._json_value(proposal["proposed_json"], {})
                target_stage = str(proposed.get("target_stage") or "")
                connection.execute(
                    """
                    INSERT INTO nursery_confirmations (subject_type, subject_id, subject_version, caregiver_id, confirmation_status, source_operation_id, created_at, updated_at)
                    VALUES ('stage_change', ?, ?, ?, 'confirmed', ?, ?, ?)
                    ON CONFLICT(subject_type, subject_id, subject_version, caregiver_id)
                    DO UPDATE SET confirmation_status='confirmed', source_operation_id=excluded.source_operation_id, updated_at=excluded.updated_at
                    """,
                    (proposal_id, str(proposal["proposal_version"]), operation["caregiver_id"], operation_id, stamp, stamp),
                )
                roles = {str(row["role"]) for row in connection.execute("""
                    SELECT DISTINCT c.role FROM nursery_confirmations n
                    JOIN nursery_caregivers c ON c.caregiver_id=n.caregiver_id
                    JOIN nursery_caregiver_profiles p ON p.caregiver_id=c.caregiver_id AND p.child_id=?
                    WHERE n.subject_type='stage_change' AND n.subject_id=? AND n.subject_version=?
                      AND n.confirmation_status='confirmed' AND c.permission_status='active' AND p.founding_guardian=1
                    """, (operation["child_id"], proposal_id, str(proposal["proposal_version"]))).fetchall()}
                complete = {CaregiverRole.USER_GUARDIAN.value, CaregiverRole.EXTERNAL_AI_GUARDIAN.value}.issubset(roles)
                if complete:
                    connection.execute("UPDATE nursery_children SET stage_id=?, updated_at=? WHERE child_id=?", (target_stage, stamp, operation["child_id"]))
                    connection.execute("UPDATE nursery_proposals SET status='confirmed', updated_at=? WHERE proposal_id=?", (stamp, proposal_id))
                    state = dict(state)
                    state["stage_id"] = target_stage
                mutation_changed = True
                result_extra.update({"proposal_id": proposal_id, "confirmation_complete": complete, "target_stage": target_stage})
            elif action == NurseryAction.WITHDRAW_STAGE_PROPOSAL:
                proposal_id = required_text("proposal_id", maximum=200)
                changed_rows = connection.execute("UPDATE nursery_proposals SET status='withdrawn', updated_at=? WHERE proposal_id=? AND child_id=? AND proposal_type='stage' AND status='open' AND proposed_by=?", (stamp, proposal_id, operation["child_id"], operation["caregiver_id"])).rowcount
                if not changed_rows:
                    raise NurseryError("PROPOSAL_NOT_WITHDRAWABLE", "stage proposal cannot be withdrawn")
                mutation_changed = True
                result_extra["proposal_id"] = proposal_id
            elif action == NurseryAction.RECORD_CARE_OBSERVATION:
                observation = payload.get("observation") or {}
                if not isinstance(observation, dict):
                    raise NurseryError("INVALID_CARE_OBSERVATION", "observation must be an object")
                behavior = " ".join(str(observation.get("observed_behavior") or "").split())
                context_summary = " ".join(str(observation.get("context_summary") or "").split())
                if not behavior or len(behavior) > 1000 or len(context_summary) > 1000:
                    raise NurseryError("INVALID_CARE_OBSERVATION", "care observation is invalid")
                health_event_id = str(payload.get("health_event_id") or "").strip() or None
                if health_event_id and not connection.execute("SELECT 1 FROM nursery_health_events WHERE health_event_id=? AND child_id=?", (health_event_id, operation["child_id"])).fetchone():
                    raise NurseryError("HEALTH_EVENT_NOT_FOUND", "health event was not found")
                observation_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"nursery:care-observation:{operation_id}"))
                connection.execute(
                    """
                    INSERT INTO nursery_health_observations (observation_id, health_event_id, child_id, caregiver_id, observed_behavior, context_summary, observed_at, source_operation_id, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (observation_id, health_event_id, operation["child_id"], operation["caregiver_id"], behavior, context_summary or None, str(observation.get("observed_at") or stamp), operation_id, stamp),
                )
                mutation_changed = True
                result_extra["observation_id"] = observation_id
            elif action == NurseryAction.COMFORT_CARE:
                health_event_id = str(payload.get("health_event_id") or "").strip() or None
                if health_event_id and not connection.execute("SELECT 1 FROM nursery_health_events WHERE health_event_id=? AND child_id=? AND status NOT IN ('recovered','corrected')", (health_event_id, operation["child_id"])).fetchone():
                    raise NurseryError("HEALTH_EVENT_NOT_FOUND", "active health event was not found")
                existing_clock = connection.execute(
                    "SELECT * FROM nursery_body_clocks WHERE child_id=?",
                    (operation["child_id"],),
                ).fetchone()
                care_clock = (
                    dict(existing_clock)
                    if existing_clock
                    else self._body_clock_payload(
                        str(operation["child_id"]),
                        stamp=stamp,
                        rule_version="body-time-disabled-v1",
                    )
                )
                care_clock.update({"last_care_at": stamp, "updated_at": stamp})
                self._save_body_clock(connection, care_clock)
                mutation_changed = True
                result_extra.update({"comfort_recorded": True, "recorded_at": stamp})
            elif action == NurseryAction.SAVE_CONFIRMED_CARE_PLAN:
                care_plan_id = required_text("care_plan_id", maximum=200)
                health_event_id = required_text("health_event_id", maximum=200)
                summary = required_text("user_confirmed_summary", maximum=1000)
                event = connection.execute(
                    """
                    SELECT health_event_id FROM nursery_health_events
                    WHERE health_event_id=? AND child_id=?
                      AND status NOT IN ('recovered', 'corrected')
                    """,
                    (health_event_id, operation["child_id"]),
                ).fetchone()
                if not event:
                    raise NurseryError(
                        "HEALTH_EVENT_NOT_ACTIVE",
                        "a confirmed care plan requires an existing active health event",
                    )
                bound_plan = connection.execute(
                    """
                    SELECT child_id, health_event_id FROM nursery_care_plans
                    WHERE care_plan_id=? LIMIT 1
                    """,
                    (care_plan_id,),
                ).fetchone()
                if bound_plan and (
                    str(bound_plan["child_id"]) != str(operation["child_id"])
                    or str(bound_plan["health_event_id"]) != health_event_id
                ):
                    raise NurseryError(
                        "CARE_PLAN_ID_CONFLICT",
                        "care_plan_id is already bound to another health event",
                    )
                prior = connection.execute(
                    """
                    SELECT plan_version FROM nursery_care_plans
                    WHERE care_plan_id=? AND child_id=? AND health_event_id=?
                    ORDER BY plan_version DESC LIMIT 1
                    """,
                    (care_plan_id, operation["child_id"], health_event_id),
                ).fetchone()
                plan_version = int(prior["plan_version"] if prior else 0) + 1
                # Do not silently replace a previous real-world agreement.
                connection.execute(
                    """
                    UPDATE nursery_care_plans SET status='withdrawn', updated_at=?
                    WHERE care_plan_id=? AND child_id=? AND health_event_id=?
                      AND status='confirmed'
                    """,
                    (stamp, care_plan_id, operation["child_id"], health_event_id),
                )
                connection.execute(
                    """
                    INSERT INTO nursery_care_plans (
                        care_plan_id, child_id, health_event_id, plan_version,
                        user_confirmed_summary, status, confirmed_by_user_at,
                        source_operation_id, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, 'confirmed', ?, ?, ?, ?)
                    """,
                    (
                        care_plan_id,
                        operation["child_id"],
                        health_event_id,
                        plan_version,
                        summary,
                        stamp,
                        operation_id,
                        stamp,
                        stamp,
                    ),
                )
                mutation_changed = True
                result_extra.update(
                    {
                        "care_plan_id": care_plan_id,
                        "care_plan_version": plan_version,
                        "care_plan_status": "confirmed",
                        "health_event_id": health_event_id,
                    }
                )
            elif action == NurseryAction.EXECUTE_CONFIRMED_CARE:
                care_plan_id = required_text("care_plan_id", maximum=200)
                try:
                    plan_version = int(payload.get("care_plan_version") or 0)
                except (TypeError, ValueError) as exc:
                    raise NurseryError(
                        "CARE_PLAN_VERSION_REQUIRED",
                        "care_plan_version must be a positive integer",
                    ) from exc
                if plan_version < 1:
                    raise NurseryError(
                        "CARE_PLAN_VERSION_REQUIRED",
                        "care_plan_version must be a positive integer",
                    )
                health_event_id = required_text("health_event_id", maximum=200)
                execution_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"nursery:care-execution:{care_plan_id}:{plan_version}:{health_event_id}"))
                execution = connection.execute(
                    """
                    SELECT e.execution_id, e.executed_at, e.caregiver_id,
                           p.display_name
                    FROM nursery_care_executions e
                    LEFT JOIN nursery_caregiver_profiles p
                      ON p.caregiver_id=e.caregiver_id AND p.child_id=e.child_id
                    WHERE e.child_id=? AND e.care_plan_id=?
                      AND e.care_plan_version=? AND e.health_event_id=?
                    """,
                    (operation["child_id"], care_plan_id, plan_version, health_event_id),
                ).fetchone()
                if execution:
                    result_extra.update(
                        {
                            "care_plan_id": care_plan_id,
                            "care_plan_version": plan_version,
                            "health_event_id": health_event_id,
                            "already_recorded": True,
                            "execution_id": str(execution["execution_id"]),
                            "executed_at": str(execution["executed_at"]),
                            "executed_by": str(execution["caregiver_id"]),
                            "executed_by_display_name": execution["display_name"],
                        }
                    )
                else:
                    plan = connection.execute(
                        """
                        SELECT 1 FROM nursery_care_plans
                        WHERE care_plan_id=? AND child_id=? AND health_event_id=?
                          AND plan_version=? AND status='confirmed'
                        """,
                        (
                            care_plan_id,
                            operation["child_id"],
                            health_event_id,
                            plan_version,
                        ),
                    ).fetchone()
                    if not plan:
                        raise NurseryError(
                            "CARE_PLAN_REQUIRED",
                            "a matching confirmed care plan is required",
                        )
                    active_event = connection.execute(
                        """
                        SELECT 1 FROM nursery_health_events
                        WHERE health_event_id=? AND child_id=?
                          AND status NOT IN ('recovered', 'corrected')
                        """,
                        (health_event_id, operation["child_id"]),
                    ).fetchone()
                    if not active_event:
                        raise NurseryError(
                            "HEALTH_EVENT_NOT_ACTIVE",
                            "a confirmed care plan cannot be first executed after its health event is resolved",
                        )
                    connection.execute(
                        """
                        INSERT INTO nursery_care_executions (
                            execution_id, care_plan_id, care_plan_version,
                            health_event_id, child_id, caregiver_id,
                            source_operation_id, executed_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            execution_id,
                            care_plan_id,
                            plan_version,
                            health_event_id,
                            operation["child_id"],
                            operation["caregiver_id"],
                            operation_id,
                            stamp,
                        ),
                    )
                    connection.execute(
                        """
                        UPDATE nursery_health_events SET status='caring', updated_at=?
                        WHERE health_event_id=? AND child_id=?
                          AND status NOT IN ('recovered', 'corrected')
                        """,
                        (stamp, health_event_id, operation["child_id"]),
                    )
                    existing_clock = connection.execute(
                        "SELECT * FROM nursery_body_clocks WHERE child_id=?",
                        (operation["child_id"],),
                    ).fetchone()
                    care_clock = (
                        dict(existing_clock)
                        if existing_clock
                        else self._body_clock_payload(
                            str(operation["child_id"]),
                            stamp=stamp,
                            rule_version="body-time-disabled-v1",
                        )
                    )
                    care_clock.update({"last_care_at": stamp, "updated_at": stamp})
                    self._save_body_clock(connection, care_clock)
                    mutation_changed = True
                    profile = connection.execute(
                        """
                        SELECT display_name FROM nursery_caregiver_profiles
                        WHERE caregiver_id=? AND child_id=?
                        """,
                        (operation["caregiver_id"], operation["child_id"]),
                    ).fetchone()
                    result_extra.update(
                        {
                            "care_plan_id": care_plan_id,
                            "care_plan_version": plan_version,
                            "health_event_id": health_event_id,
                            "already_recorded": False,
                            "execution_id": execution_id,
                            "executed_at": stamp,
                            "executed_by": operation["caregiver_id"],
                            "executed_by_display_name": (
                                profile["display_name"] if profile else None
                            ),
                        }
                    )
            elif action == NurseryAction.REQUEST_DELETION:
                proposal_id, proposal_version = create_proposal("delete", {"requested_from_state_version": before_version})
                mutation_changed = True
                result_extra.update({"delete_proposal_id": proposal_id, "delete_proposal_version": proposal_version})
            elif action == NurseryAction.PROPOSE_NAME_CHANGE:
                formal_name = optional_text("formal_name", maximum=80)
                nickname = optional_text("nickname", maximum=80)
                if not formal_name and not nickname:
                    raise NurseryError("INVALID_NAME_PROPOSAL", "formal_name or nickname is required")
                proposal_id, proposal_version = create_proposal("name", {"formal_name": formal_name, "nickname": nickname})
                mutation_changed = True
                result_extra.update({"proposal_id": proposal_id, "proposal_version": proposal_version})
            elif action == NurseryAction.CONFIRM_NAME_CHANGE:
                proposal_id = required_text("proposal_id", maximum=200)
                proposal = connection.execute("SELECT * FROM nursery_proposals WHERE proposal_id=? AND child_id=? AND proposal_type='name' AND status='open'", (proposal_id, operation["child_id"])).fetchone()
                if not proposal or int(payload.get("proposal_version") or 0) != int(proposal["proposal_version"]):
                    raise NurseryError("PROPOSAL_VERSION_CONFLICT", "name proposal is not current")
                connection.execute(
                    """
                    INSERT INTO nursery_confirmations (subject_type, subject_id, subject_version, caregiver_id, confirmation_status, source_operation_id, created_at, updated_at)
                    VALUES ('name_change', ?, ?, ?, 'confirmed', ?, ?, ?)
                    ON CONFLICT(subject_type, subject_id, subject_version, caregiver_id)
                    DO UPDATE SET confirmation_status='confirmed', source_operation_id=excluded.source_operation_id, updated_at=excluded.updated_at
                    """,
                    (proposal_id, str(proposal["proposal_version"]), operation["caregiver_id"], operation_id, stamp, stamp),
                )
                roles = {str(row["role"]) for row in connection.execute("""
                    SELECT DISTINCT c.role FROM nursery_confirmations n JOIN nursery_caregivers c ON c.caregiver_id=n.caregiver_id JOIN nursery_caregiver_profiles p ON p.caregiver_id=c.caregiver_id
                    WHERE n.subject_type='name_change' AND n.subject_id=? AND n.subject_version=? AND n.confirmation_status='confirmed' AND c.permission_status='active' AND p.founding_guardian=1
                    """, (proposal_id, str(proposal["proposal_version"]))).fetchall()}
                complete = {CaregiverRole.USER_GUARDIAN.value, CaregiverRole.EXTERNAL_AI_GUARDIAN.value}.issubset(roles)
                proposed = self._json_value(proposal["proposed_json"], {})
                if complete:
                    identity = connection.execute("SELECT official_name, nickname FROM nursery_child_identity WHERE child_id=?", (operation["child_id"],)).fetchone()
                    formal_name = str(proposed.get("formal_name") or identity["official_name"])
                    nickname = proposed.get("nickname") if proposed.get("nickname") is not None else identity["nickname"]
                    connection.execute("UPDATE nursery_child_identity SET official_name=?, nickname=? WHERE child_id=?", (formal_name, nickname, operation["child_id"]))
                    for kind, value in (("official", formal_name), ("nickname", nickname)):
                        if not value:
                            continue
                        connection.execute("UPDATE nursery_name_history SET valid_until=? WHERE child_id=? AND name_kind=? AND valid_until IS NULL", (stamp, operation["child_id"], kind))
                        connection.execute("INSERT INTO nursery_name_history (child_id, name_kind, name_value, valid_from, reason_code, source_operation_id) VALUES (?, ?, ?, ?, 'shared_change', ?)", (operation["child_id"], kind, value, stamp, operation_id))
                    connection.execute("UPDATE nursery_proposals SET status='confirmed', updated_at=? WHERE proposal_id=?", (stamp, proposal_id))
                mutation_changed = True
                result_extra.update({"proposal_id": proposal_id, "confirmation_complete": complete})
            elif action == NurseryAction.WITHDRAW_NAME_CHANGE:
                proposal_id = required_text("proposal_id", maximum=200)
                if not connection.execute("UPDATE nursery_proposals SET status='withdrawn', updated_at=? WHERE proposal_id=? AND child_id=? AND proposal_type='name' AND status='open' AND proposed_by=?", (stamp, proposal_id, operation["child_id"], operation["caregiver_id"])).rowcount:
                    raise NurseryError("PROPOSAL_NOT_WITHDRAWABLE", "name proposal cannot be withdrawn")
                mutation_changed = True
                result_extra["proposal_id"] = proposal_id
            elif action == NurseryAction.UPDATE_OWN_CALLING_PREFERENCE:
                value = required_text("calling_preference", maximum=80)
                profile = connection.execute("SELECT display_name FROM nursery_caregiver_profiles WHERE caregiver_id=? AND child_id=?", (operation["caregiver_id"], operation["child_id"])).fetchone()
                if not profile:
                    raise NurseryError("CAREGIVER_PROFILE_NOT_FOUND", "caregiver profile was not found")
                connection.execute("UPDATE nursery_caregiver_profiles SET display_name=?, updated_at=? WHERE caregiver_id=? AND child_id=?", (value, stamp, operation["caregiver_id"], operation["child_id"]))
                mutation_changed = True
                result_extra["calling_preference"] = value
            elif action == NurseryAction.SYNC_ANIMA_CONTEXT:
                context = prepared.get("anima_context")
                if not isinstance(context, dict):
                    raise NurseryError(
                        "ANIMA_CONTEXT_PREPARATION_REQUIRED",
                        "a validated Anima child context is required",
                    )
                saved_context = self._save_child_safe_anima_context_in_transaction(
                    connection,
                    child_id=str(operation["child_id"]),
                    context=context,
                )
                mutation_changed = bool(saved_context["accepted"])
                result_extra["anima_context_id"] = saved_context["context_id"]
                result_extra["anima_context_accepted"] = bool(saved_context["accepted"])
            elif action == NurseryAction.APPLY_ANIMA_FAMILY_EVENT:
                required = {
                    "experience_id",
                    "context",
                    "occurred_at",
                    "category",
                    "summary",
                    "child_reaction",
                    "current_ripple",
                    "requires_attention",
                }
                if not required.issubset(prepared):
                    raise NurseryError(
                        "ANIMA_FAMILY_EVENT_PREPARATION_REQUIRED",
                        "a validated Anima family-event proposal is required",
                    )
                context = prepared["context"]
                if not isinstance(context, dict):
                    raise NurseryError(
                        "INVALID_ANIMA_FAMILY_EVENT",
                        "Anima family-event context is invalid",
                    )
                saved_context = self._save_child_safe_anima_context_in_transaction(
                    connection,
                    child_id=str(operation["child_id"]),
                    context=context,
                )
                experience_id = str(prepared["experience_id"])
                category = str(prepared["category"])
                occurred_at = str(prepared["occurred_at"])
                summary = str(prepared["summary"])
                reaction = str(prepared["child_reaction"])
                ripple = prepared["current_ripple"]
                if (
                    not experience_id
                    or category != "family"
                    or not occurred_at
                    or not summary
                    or not reaction
                    or len(summary) > 240
                    or len(reaction) > 240
                    or (ripple is not None and (not isinstance(ripple, str) or len(ripple) > 240))
                ):
                    raise NurseryError(
                        "INVALID_ANIMA_FAMILY_EVENT",
                        "Anima family-event proposal is invalid",
                    )
                connection.execute(
                    """
                    INSERT INTO nursery_experiences (
                        experience_id, child_id, category, occurred_at,
                        age_appropriate_summary, child_reaction, resolution,
                        requires_attention, created_at, updated_at
                    ) VALUES (?, ?, 'family', ?, ?, ?, 'unresolved', ?, ?, ?)
                    """,
                    (
                        experience_id,
                        operation["child_id"],
                        occurred_at,
                        summary,
                        reaction,
                        int(prepared["requires_attention"] is True),
                        stamp,
                        stamp,
                    ),
                )
                source = connection.execute(
                    """
                    SELECT source_type, source_id, source_version
                    FROM nursery_source_events WHERE operation_id=?
                    """,
                    (operation_id,),
                ).fetchone()
                if not source:
                    raise NurseryError("SOURCE_REQUIRED", "family event source is missing")
                connection.execute(
                    """
                    INSERT INTO nursery_experience_sources (
                        experience_id, source_type, source_id, source_version, source_validity
                    ) VALUES (?, ?, ?, ?, 'valid')
                    """,
                    (
                        experience_id,
                        source["source_type"],
                        source["source_id"],
                        source["source_version"],
                    ),
                )
                connection.execute(
                    """
                    INSERT INTO nursery_experience_ripples (
                        experience_id, current_ripple, active, recalculation_version, updated_at
                    ) VALUES (?, ?, ?, 'anima-family-event-v1', ?)
                    """,
                    (experience_id, ripple, int(bool(ripple)), stamp),
                )
                mutation_changed = True
                result_extra.update(
                    {
                        "anima_context_id": saved_context["context_id"],
                        "experience_id": experience_id,
                        "family_event_applied": True,
                    }
                )
            elif action == NurseryAction.BIND_EXTERNAL_GUARDIAN:
                external_id = str(payload.get("caregiver_id") or "").strip()
                display_name = str(payload.get("display_name") or "").strip()
                if not external_id or len(external_id) > 200:
                    raise NurseryError(
                        "INVALID_CAREGIVER_ID", "external caregiver id is required"
                    )
                if not display_name or len(display_name) > 80:
                    raise NurseryError(
                        "INVALID_DISPLAY_NAME", "external caregiver display name is required"
                    )
                existing_external = connection.execute(
                    """
                    SELECT c.caregiver_id FROM nursery_caregivers c
                    JOIN nursery_caregiver_profiles p
                      ON p.caregiver_id=c.caregiver_id
                    WHERE c.account_id=? AND c.role='external_ai_guardian'
                      AND c.permission_status='active' AND p.founding_guardian=1
                    """,
                    (operation["account_id"],),
                ).fetchone()
                if existing_external and existing_external["caregiver_id"] != external_id:
                    raise NurseryError(
                        "FOUNDING_GUARDIAN_ALREADY_BOUND",
                        "the founding external AI guardian is already bound",
                    )
                identity_collision = connection.execute(
                    "SELECT account_id, role FROM nursery_caregivers WHERE caregiver_id=?",
                    (external_id,),
                ).fetchone()
                if identity_collision and (
                    identity_collision["account_id"] != operation["account_id"]
                    or identity_collision["role"]
                    != CaregiverRole.EXTERNAL_AI_GUARDIAN.value
                ):
                    raise NurseryError(
                        "CAREGIVER_ID_CONFLICT",
                        "caregiver id is bound to another account or role",
                    )
                connection.execute(
                    """
                    INSERT INTO nursery_caregivers (
                        caregiver_id, account_id, role, permission_status,
                        created_at, updated_at
                    ) VALUES (?, ?, 'external_ai_guardian', 'active', ?, ?)
                    ON CONFLICT(caregiver_id) DO UPDATE SET
                        permission_status='active', updated_at=excluded.updated_at
                    """,
                    (external_id, operation["account_id"], stamp, stamp),
                )
                connection.execute(
                    """
                    INSERT INTO nursery_caregiver_profiles (
                        caregiver_id, child_id, display_name, founding_guardian,
                        created_at, updated_at
                    ) VALUES (?, ?, ?, 1, ?, ?)
                    ON CONFLICT(caregiver_id) DO UPDATE SET
                        display_name=excluded.display_name,
                        founding_guardian=1, updated_at=excluded.updated_at
                    """,
                    (external_id, operation["child_id"], display_name, stamp, stamp),
                )
                draft_version = self._bump_draft_version(
                    connection, operation["child_id"], stamp
                )
                mutation_changed = True
                result_extra["external_guardian_bound"] = True
            elif action in {
                NurseryAction.SAVE_MODEL_CONNECTION,
                NurseryAction.RETEST_MODEL_CONNECTION,
            }:
                required = {
                    "provider_id",
                    "base_url",
                    "model_name",
                    "credential_ref",
                    "credential_suffix",
                    "capabilities",
                    "tested_at",
                }
                if not required.issubset(prepared):
                    raise NurseryError(
                        "MODEL_PREPARATION_REQUIRED",
                        "tested model configuration is required",
                    )
                current_config = connection.execute(
                    "SELECT config_version FROM nursery_model_configs "
                    "WHERE account_id=?",
                    (operation["account_id"],),
                ).fetchone()
                if prepared.get("retest") and current_config:
                    config_version = int(current_config["config_version"])
                else:
                    config_version = (
                        int(current_config["config_version"] + 1)
                        if current_config
                        else 1
                    )
                connection.execute(
                    """
                    INSERT INTO nursery_model_configs (
                        account_id, provider_id, base_url, model_name,
                        credential_ref, credential_suffix, connection_status,
                        capabilities_json, config_version, tested_at,
                        created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, 'ready', ?, ?, ?, ?, ?)
                    ON CONFLICT(account_id) DO UPDATE SET
                        provider_id=excluded.provider_id,
                        base_url=excluded.base_url,
                        model_name=excluded.model_name,
                        credential_ref=excluded.credential_ref,
                        credential_suffix=excluded.credential_suffix,
                        connection_status='ready',
                        capabilities_json=excluded.capabilities_json,
                        config_version=excluded.config_version,
                        tested_at=excluded.tested_at,
                        updated_at=excluded.updated_at
                    """,
                    (
                        operation["account_id"],
                        prepared["provider_id"],
                        prepared["base_url"],
                        prepared["model_name"],
                        prepared["credential_ref"],
                        prepared["credential_suffix"],
                        canonical_json(prepared["capabilities"]),
                        config_version,
                        prepared["tested_at"],
                        stamp,
                        stamp,
                    ),
                )
                mutation_changed = True
                result_extra.update(
                    {
                        "model_connection": "ready",
                        "model_config_version": config_version,
                        "model_retested": bool(prepared.get("retest")),
                    }
                )
            elif action == NurseryAction.DELETE_MODEL_CONNECTION:
                credential_ref = str(prepared.get("credential_ref") or "")
                current_config = connection.execute(
                    "SELECT credential_ref FROM nursery_model_configs WHERE account_id=?",
                    (operation["account_id"],),
                ).fetchone()
                if not current_config:
                    raise NurseryError(
                        "MODEL_NOT_CONFIGURED", "child model connection does not exist"
                    )
                if credential_ref != str(current_config["credential_ref"]):
                    raise NurseryError(
                        "MODEL_CONFIG_CHANGED",
                        "model connection changed before deletion could commit",
                    )
                connection.execute(
                    "DELETE FROM nursery_model_configs WHERE account_id=?",
                    (operation["account_id"],),
                )
                mutation_changed = True
                result_extra["model_connection"] = "deleted"
            elif action == NurseryAction.SAVE_DRAFT_IDENTITY:
                sex_status = str(payload.get("sex_status") or "").strip()
                stage_id = str(payload.get("stage_id") or "").strip()
                nickname = str(payload.get("nickname") or "").strip()
                address_terms = payload.get("address_terms") or {}
                if sex_status not in {"boy", "girl", "neutral", "undecided"}:
                    raise NurseryError("INVALID_SEX_STATUS", "sex status is invalid")
                if stage_id not in {
                    "infancy",
                    "early_walker",
                    "toddler",
                    "young_child",
                }:
                    raise NurseryError(
                        "INVALID_STAGE", "stage must come from the capability YAML"
                    )
                if len(nickname) > 80 or not isinstance(address_terms, dict):
                    raise NurseryError("INVALID_IDENTITY", "draft identity is invalid")
                clean_terms: dict[str, str] = {}
                for key, value in address_terms.items():
                    key_text = str(key).strip()
                    value_text = str(value).strip()
                    if not key_text or len(key_text) > 80 or len(value_text) > 80:
                        raise NurseryError(
                            "INVALID_ADDRESS_TERM", "address term is invalid"
                        )
                    clean_terms[key_text] = value_text
                connection.execute(
                    """
                    UPDATE nursery_creation_drafts
                    SET sex_status=?, stage_id=?, nickname=?, address_terms_json=?,
                        updated_at=? WHERE child_id=?
                    """,
                    (
                        sex_status,
                        stage_id,
                        nickname or None,
                        canonical_json(clean_terms),
                        stamp,
                        operation["child_id"],
                    ),
                )
                connection.execute(
                    "UPDATE nursery_children SET stage_id=?, updated_at=? WHERE child_id=?",
                    (stage_id, stamp, operation["child_id"]),
                )
                draft_version = self._bump_draft_version(
                    connection, operation["child_id"], stamp
                )
                mutation_changed = True
            elif action == NurseryAction.SAVE_NAME_PROPOSALS:
                proposals = payload.get("proposals")
                if not isinstance(proposals, list) or not 1 <= len(proposals) <= 3:
                    raise NurseryError(
                        "INVALID_NAME_PROPOSALS", "one to three name proposals are required"
                    )
                old_candidate_ids = [
                    str(row["candidate_id"])
                    for row in connection.execute(
                        """
                        SELECT candidate_id FROM nursery_name_candidates
                        WHERE child_id=? AND caregiver_id=?
                        """,
                        (operation["child_id"], operation["caregiver_id"]),
                    ).fetchall()
                ]
                if old_candidate_ids:
                    placeholders = ",".join("?" for _ in old_candidate_ids)
                    connection.execute(
                        f"""
                        DELETE FROM nursery_name_preferences
                        WHERE child_id=? AND candidate_id IN ({placeholders})
                        """,
                        (operation["child_id"], *old_candidate_ids),
                    )
                connection.execute(
                    "DELETE FROM nursery_name_candidates WHERE child_id=? AND caregiver_id=?",
                    (operation["child_id"], operation["caregiver_id"]),
                )
                for ordinal, proposal in enumerate(proposals, 1):
                    if not isinstance(proposal, dict):
                        raise NurseryError(
                            "INVALID_NAME_PROPOSALS", "name proposal must be an object"
                        )
                    proposed_name = str(proposal.get("name") or "").strip()
                    if not proposed_name or len(proposed_name) > 80:
                        raise NurseryError("INVALID_NAME_PROPOSALS", "candidate name is required")
                    expected_candidate_id = name_candidate_id(
                        operation["child_id"],
                        operation["caregiver_id"],
                        ordinal,
                        proposed_name,
                    )
                    candidate_id = str(proposal.get("candidate_id") or expected_candidate_id).strip()
                    if candidate_id != name_candidate_id(
                        operation["child_id"],
                        operation["caregiver_id"],
                        ordinal,
                        proposed_name,
                    ):
                        raise NurseryError(
                            "INVALID_NAME_CANDIDATE_ID",
                            "name candidate id must match its immutable source",
                        )
                    notes = []
                    for key in ("meaning", "sound_notes", "avoid_notes"):
                        value = str(proposal.get(key) or "").strip()
                        if len(value) > 500:
                            raise NurseryError(
                                "INVALID_NAME_PROPOSALS", "name proposal text is too long"
                            )
                        notes.append(value)
                    connection.execute(
                        """
                        INSERT INTO nursery_name_candidates (
                            candidate_id, child_id, caregiver_id, ordinal,
                            proposed_name, meaning_text, sound_notes, avoid_notes,
                            created_at, updated_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            candidate_id,
                            operation["child_id"],
                            operation["caregiver_id"],
                            ordinal,
                            proposed_name,
                            *notes,
                            stamp,
                            stamp,
                        ),
                    )
                connection.execute(
                    """
                    UPDATE nursery_creation_drafts
                    SET selected_candidate_id=NULL, official_name=NULL, updated_at=?
                    WHERE child_id=?
                    """,
                    (stamp, operation["child_id"]),
                )
                draft_version = self._bump_draft_version(
                    connection, operation["child_id"], stamp
                )
                mutation_changed = True
            elif action == NurseryAction.REVIEW_NAME_CANDIDATES:
                preferences = payload.get("preferences")
                if not isinstance(preferences, dict) or not preferences:
                    raise NurseryError(
                        "INVALID_NAME_PREFERENCES", "name preferences are required"
                    )
                candidates = {
                    str(row["candidate_id"])
                    for row in connection.execute(
                        "SELECT candidate_id FROM nursery_name_candidates WHERE child_id=?",
                        (operation["child_id"],),
                    ).fetchall()
                }
                if set(preferences) != candidates:
                    raise NurseryError(
                        "NAME_REVIEW_INCOMPLETE",
                        "every current name candidate must be reviewed",
                    )
                connection.execute(
                    "DELETE FROM nursery_name_preferences WHERE child_id=? AND reviewer_id=?",
                    (operation["child_id"], operation["caregiver_id"]),
                )
                for candidate_id, preference in preferences.items():
                    value = str(preference).strip()
                    if value == "disagree":
                        value = "reject"
                    if value not in {"like", "acceptable", "reject"}:
                        raise NurseryError(
                            "INVALID_NAME_PREFERENCE", "name preference is invalid"
                        )
                    connection.execute(
                        """
                        INSERT INTO nursery_name_preferences (
                            child_id, reviewer_id, candidate_id, preference,
                            created_at, updated_at
                        ) VALUES (?, ?, ?, ?, ?, ?)
                        """,
                        (
                            operation["child_id"],
                            operation["caregiver_id"],
                            candidate_id,
                            value,
                            stamp,
                            stamp,
                        ),
                    )
                official_name = self._settle_shared_name(
                    connection, operation["child_id"], stamp
                )
                draft_version = self._bump_draft_version(
                    connection, operation["child_id"], stamp
                )
                mutation_changed = True
                if official_name:
                    result_extra["official_name"] = official_name
                    result_extra["name_auto_selected"] = True
            elif action == NurseryAction.SELECT_DRAFT_NAME:
                candidate_id = str(payload.get("candidate_id") or "").strip()
                candidate = connection.execute(
                    """
                    SELECT proposed_name FROM nursery_name_candidates
                    WHERE child_id=? AND candidate_id=?
                    """,
                    (operation["child_id"], candidate_id),
                ).fetchone()
                if not candidate:
                    raise NurseryError("NAME_CANDIDATE_NOT_FOUND", "name candidate not found")
                accepted_roles = {
                    str(row["role"])
                    for row in connection.execute(
                        """
                        SELECT DISTINCT c.role FROM nursery_name_preferences n
                        JOIN nursery_caregivers c ON c.caregiver_id=n.reviewer_id
                        JOIN nursery_caregiver_profiles p ON p.caregiver_id=c.caregiver_id
                        WHERE n.child_id=? AND n.candidate_id=?
                          AND n.preference IN ('like', 'acceptable')
                          AND p.founding_guardian=1 AND c.permission_status='active'
                        """,
                        (operation["child_id"], candidate_id),
                    ).fetchall()
                }
                if not {
                    CaregiverRole.USER_GUARDIAN.value,
                    CaregiverRole.EXTERNAL_AI_GUARDIAN.value,
                }.issubset(accepted_roles):
                    raise NurseryError(
                        "NAME_CONSENSUS_REQUIRED",
                        "both founding guardians must accept the selected name",
                    )
                connection.execute(
                    """
                    UPDATE nursery_creation_drafts
                    SET selected_candidate_id=?, official_name=?, updated_at=?
                    WHERE child_id=?
                    """,
                    (
                        candidate_id,
                        candidate["proposed_name"],
                        stamp,
                        operation["child_id"],
                    ),
                )
                draft_version = self._bump_draft_version(
                    connection, operation["child_id"], stamp
                )
                mutation_changed = True
                result_extra["official_name"] = str(candidate["proposed_name"])
            elif action in {
                NurseryAction.SUBMIT_TEMPERAMENT_QUESTIONNAIRE,
                NurseryAction.SUBMIT_INITIAL_STYLE,
            }:
                required = {
                    "questionnaire_kind",
                    "questionnaire_version",
                    "answer_digest",
                    "private_ref",
                    "normalized",
                }
                if not required.issubset(prepared):
                    raise NurseryError(
                        "QUESTIONNAIRE_PREPARATION_REQUIRED",
                        "validated private questionnaire material is required",
                    )
                expected_kind = (
                    QuestionnaireKind.TEMPERAMENT.value
                    if action == NurseryAction.SUBMIT_TEMPERAMENT_QUESTIONNAIRE
                    else QuestionnaireKind.INITIAL_STYLE.value
                )
                if prepared["questionnaire_kind"] != expected_kind:
                    raise NurseryError(
                        "QUESTIONNAIRE_KIND_MISMATCH", "questionnaire kind is invalid"
                    )
                connection.execute(
                    """
                    INSERT INTO nursery_private_submissions (
                        child_id, caregiver_id, questionnaire_kind,
                        questionnaire_version, answer_digest, private_ref,
                        normalized_json, submitted_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(child_id, caregiver_id, questionnaire_kind)
                    DO UPDATE SET questionnaire_version=excluded.questionnaire_version,
                        answer_digest=excluded.answer_digest,
                        private_ref=excluded.private_ref,
                        normalized_json=excluded.normalized_json,
                        submitted_at=excluded.submitted_at,
                        updated_at=excluded.updated_at
                    """,
                    (
                        operation["child_id"],
                        operation["caregiver_id"],
                        expected_kind,
                        prepared["questionnaire_version"],
                        prepared["answer_digest"],
                        prepared["private_ref"],
                        canonical_json(prepared["normalized"]),
                        stamp,
                        stamp,
                    ),
                )
                if expected_kind == QuestionnaireKind.INITIAL_STYLE.value:
                    connection.execute(
                        """
                        UPDATE nursery_caregiver_profiles
                        SET declared_initial_style_json=?, updated_at=?
                        WHERE caregiver_id=? AND child_id=?
                        """,
                        (
                            canonical_json(prepared["normalized"]),
                            stamp,
                            operation["caregiver_id"],
                            operation["child_id"],
                        ),
                    )
                else:
                    vectors = {
                        str(row["role"]): json.loads(row["normalized_json"])
                        for row in connection.execute(
                            """
                            SELECT c.role, s.normalized_json
                            FROM nursery_private_submissions s
                            JOIN nursery_caregivers c ON c.caregiver_id=s.caregiver_id
                            JOIN nursery_caregiver_profiles p
                              ON p.caregiver_id=c.caregiver_id
                            WHERE s.child_id=? AND s.questionnaire_kind='temperament'
                              AND c.permission_status='active' AND p.founding_guardian=1
                            """,
                            (operation["child_id"],),
                        ).fetchall()
                    }
                    if {
                        CaregiverRole.USER_GUARDIAN.value,
                        CaregiverRole.EXTERNAL_AI_GUARDIAN.value,
                    }.issubset(vectors):
                        draft = connection.execute(
                            """
                            SELECT independent_temperament_json,
                                   temperament_formula_version
                            FROM nursery_creation_drafts WHERE child_id=?
                            """,
                            (operation["child_id"],),
                        ).fetchone()
                        final_vector = blend_temperament(
                            vectors[CaregiverRole.USER_GUARDIAN.value],
                            vectors[CaregiverRole.EXTERNAL_AI_GUARDIAN.value],
                            json.loads(draft["independent_temperament_json"]),
                        )
                        summary = temperament_summary(final_vector)
                        connection.execute(
                            """
                            INSERT INTO nursery_temperament_profiles (
                                child_id, formula_version, final_vector_json,
                                summary_text, calculated_at
                            ) VALUES (?, ?, ?, ?, ?)
                            ON CONFLICT(child_id) DO UPDATE SET
                                formula_version=excluded.formula_version,
                                final_vector_json=excluded.final_vector_json,
                                summary_text=excluded.summary_text,
                                calculated_at=excluded.calculated_at,
                                sealed_at=NULL
                            """,
                            (
                                operation["child_id"],
                                draft["temperament_formula_version"],
                                canonical_json(final_vector),
                                summary,
                                stamp,
                            ),
                        )
                        result_extra["temperament_ready"] = True
                draft_version = self._bump_draft_version(
                    connection, operation["child_id"], stamp
                )
                mutation_changed = True
            elif action == NurseryAction.SAVE_INITIAL_SPACE:
                space = payload.get("space")
                allowed_fields = {
                    "room_overall",
                    "walls",
                    "floor",
                    "window",
                    "sleeping_place",
                    "bedding",
                    "lighting",
                    "storage",
                    "other_furniture",
                    "custom_content",
                }
                if not isinstance(space, dict) or not set(space).issubset(allowed_fields):
                    raise NurseryError("INVALID_INITIAL_SPACE", "initial space is invalid")
                clean_space: dict[str, str] = {}
                for key, value in space.items():
                    text = str(value).strip()
                    if len(text) > 2000:
                        raise NurseryError(
                            "INVALID_INITIAL_SPACE", "initial space text is too long"
                        )
                    clean_space[str(key)] = text
                connection.execute(
                    """
                    UPDATE nursery_creation_drafts
                    SET initial_space_json=?, initial_space_completed=1, updated_at=?
                    WHERE child_id=?
                    """,
                    (canonical_json(clean_space), stamp, operation["child_id"]),
                )
                draft_version = self._bump_draft_version(
                    connection, operation["child_id"], stamp
                )
                mutation_changed = True
            elif action == NurseryAction.CONFIRM_CREATION:
                draft = connection.execute(
                    "SELECT * FROM nursery_creation_drafts WHERE child_id=?",
                    (operation["child_id"],),
                ).fetchone()
                subject_version = str(payload.get("subject_version") or "")
                if subject_version != str(draft["draft_version"]):
                    raise NurseryError(
                        "CONFIRMATION_VERSION_CONFLICT",
                        "creation confirmation does not match the current draft",
                    )
                missing = self._creation_readiness(
                    connection,
                    account_id=operation["account_id"],
                    child_id=operation["child_id"],
                )
                if missing:
                    raise NurseryError(
                        "CREATION_NOT_READY",
                        "creation draft is missing required completed sections",
                        details={"missing": missing},
                    )
                connection.execute(
                    """
                    INSERT INTO nursery_confirmations (
                        subject_type, subject_id, subject_version, caregiver_id,
                        confirmation_status, source_operation_id, created_at, updated_at
                    ) VALUES ('child_creation', ?, ?, ?, 'confirmed', ?, ?, ?)
                    ON CONFLICT(subject_type, subject_id, subject_version, caregiver_id)
                    DO UPDATE SET confirmation_status='confirmed',
                        source_operation_id=excluded.source_operation_id,
                        updated_at=excluded.updated_at
                    """,
                    (
                        operation["child_id"],
                        subject_version,
                        operation["caregiver_id"],
                        operation_id,
                        stamp,
                        stamp,
                    ),
                )
                creation_confirmed = self._creation_confirmation_complete(
                    connection,
                    child_id=operation["child_id"],
                    subject_version=subject_version,
                )
                result_extra["confirmation_complete"] = creation_confirmed
                result_extra["draft_version"] = int(draft["draft_version"])
                if creation_confirmed:
                    temperament = connection.execute(
                        """
                        SELECT final_vector_json, formula_version
                        FROM nursery_temperament_profiles WHERE child_id=?
                        """,
                        (operation["child_id"],),
                    ).fetchone()
                    connection.execute(
                        """
                        INSERT INTO nursery_child_identity (
                            child_id, child_kind, sex_status, official_name,
                            nickname, address_terms_json, temperament_json,
                            temperament_formula_version, locked_at
                        ) VALUES (?, 'human', ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            operation["child_id"],
                            draft["sex_status"],
                            draft["official_name"],
                            draft["nickname"],
                            draft["address_terms_json"],
                            temperament["final_vector_json"],
                            temperament["formula_version"],
                            stamp,
                        ),
                    )
                    connection.execute(
                        """
                        INSERT INTO nursery_name_history (
                            child_id, name_kind, name_value, valid_from,
                            reason_code, source_operation_id
                        ) VALUES (?, 'official', ?, ?, 'initial_creation', ?)
                        """,
                        (
                            operation["child_id"],
                            draft["official_name"],
                            stamp,
                            operation_id,
                        ),
                    )
                    if draft["nickname"]:
                        connection.execute(
                            """
                            INSERT INTO nursery_name_history (
                                child_id, name_kind, name_value, valid_from,
                                reason_code, source_operation_id
                            ) VALUES (?, 'nickname', ?, ?, 'initial_creation', ?)
                            """,
                            (
                                operation["child_id"],
                                draft["nickname"],
                                stamp,
                                operation_id,
                            ),
                        )
                    connection.execute(
                        """
                        UPDATE nursery_creation_drafts
                        SET draft_status='sealed', sealed_at=?, updated_at=?
                        WHERE child_id=?
                        """,
                        (stamp, stamp, operation["child_id"]),
                    )
                    connection.execute(
                        """
                        UPDATE nursery_temperament_profiles SET sealed_at=?
                        WHERE child_id=?
                        """,
                        (stamp, operation["child_id"]),
                    )
                    connection.execute(
                        """
                        UPDATE nursery_private_submissions
                        SET private_ref='purged', updated_at=? WHERE child_id=?
                        """,
                        (stamp, operation["child_id"]),
                    )
                    guardians = connection.execute(
                        """
                        SELECT c.caregiver_id FROM nursery_caregivers c
                        JOIN nursery_caregiver_profiles p
                          ON p.caregiver_id=c.caregiver_id
                        WHERE c.account_id=? AND c.permission_status='active'
                          AND p.child_id=? AND p.founding_guardian=1
                        """,
                        (operation["account_id"], operation["child_id"]),
                    ).fetchall()
                    for guardian in guardians:
                        connection.execute(
                            """
                            INSERT INTO nursery_relationships (
                                child_id, caregiver_id, trust, familiarity,
                                closeness, repair, created_at, updated_at
                            ) VALUES (?, ?, 0.30, 0.40, 0.20, 0.00, ?, ?)
                            """,
                            (
                                operation["child_id"],
                                guardian["caregiver_id"],
                                stamp,
                                stamp,
                            ),
                        )
                    connection.execute(
                        """
                        INSERT INTO nursery_lifecycle_events (
                            account_id, child_id, event_type, operation_id,
                            event_json, created_at
                        ) VALUES (?, ?, 'child_created', ?, ?, ?)
                        """,
                        (
                            operation["account_id"],
                            operation["child_id"],
                            operation_id,
                            canonical_json(
                                {
                                    "official_name": draft["official_name"],
                                    "stage_id": draft["stage_id"],
                                    "confirmed_by": "two_founding_guardians",
                                }
                            ),
                            stamp,
                        ),
                    )
            elif action == NurseryAction.PAUSE:
                existing_marker = connection.execute(
                    """
                    SELECT marker_id FROM nursery_pause_markers
                    WHERE child_id=? AND caregiver_id=? AND released_at IS NULL
                    """,
                    (operation["child_id"], operation["caregiver_id"]),
                ).fetchone()
                if not existing_marker:
                    connection.execute(
                        """
                        INSERT INTO nursery_pause_markers (
                            child_id, caregiver_id, added_at, source_operation_id
                        ) VALUES (?, ?, ?, ?)
                        """,
                        (
                            operation["child_id"],
                            operation["caregiver_id"],
                            stamp,
                            operation_id,
                        ),
                    )
                    active_pause_count += 1
            elif action == NurseryAction.RESUME:
                released = connection.execute(
                    """
                    UPDATE nursery_pause_markers SET released_at=?
                    WHERE child_id=? AND caregiver_id=? AND released_at IS NULL
                    """,
                    (stamp, operation["child_id"], operation["caregiver_id"]),
                ).rowcount
                if released:
                    active_pause_count = max(0, active_pause_count - released)
            elif action == NurseryAction.CONFIRM_DELETION:
                proposal_id = str(payload.get("delete_proposal_id") or "").strip()
                proposal_version = int(payload.get("expected_delete_proposal_version") or 0)
                legacy_subject_version = str(payload.get("subject_version") or "").strip()
                if not proposal_id and legacy_subject_version:
                    proposal_id = f"legacy-delete:{operation['child_id']}:{legacy_subject_version}"
                    proposal_version = int(legacy_subject_version)
                    connection.execute(
                        """
                        INSERT OR IGNORE INTO nursery_proposals (
                            proposal_id, child_id, proposal_type, proposal_version,
                            proposed_json, status, proposed_by, source_operation_id,
                            created_at, updated_at
                        ) VALUES (?, ?, 'delete', ?, ?, 'open', ?, ?, ?, ?)
                        """,
                        (
                            proposal_id, operation["child_id"], proposal_version,
                            canonical_json({"requested_from_state_version": proposal_version}),
                            operation["caregiver_id"], operation_id, stamp, stamp,
                        ),
                    )
                proposal = connection.execute(
                    "SELECT * FROM nursery_proposals WHERE proposal_id=? AND child_id=? AND proposal_type='delete' AND status='open'",
                    (proposal_id, operation["child_id"]),
                ).fetchone()
                if not proposal or proposal_version != int(proposal["proposal_version"]):
                    raise NurseryError(
                        "CONFIRMATION_VERSION_CONFLICT",
                        "deletion confirmation does not match the current proposal",
                    )
                subject_version = str(proposal_version)
                connection.execute(
                    """
                    INSERT INTO nursery_confirmations (
                        subject_type, subject_id, subject_version, caregiver_id,
                        confirmation_status, source_operation_id, created_at, updated_at
                    ) VALUES ('child_deletion', ?, ?, ?, 'confirmed', ?, ?, ?)
                    ON CONFLICT(subject_type, subject_id, subject_version, caregiver_id)
                    DO UPDATE SET confirmation_status='confirmed',
                        source_operation_id=excluded.source_operation_id,
                        updated_at=excluded.updated_at
                    """,
                    (
                        operation["child_id"],
                        subject_version,
                        operation["caregiver_id"],
                        operation_id,
                        stamp,
                        stamp,
                    ),
                )
                deletion_confirmed = self._confirmation_complete(
                    connection,
                    subject_id=operation["child_id"],
                    subject_version=subject_version,
                )
                if deletion_confirmed:
                    connection.execute("UPDATE nursery_proposals SET status='confirmed', updated_at=? WHERE proposal_id=?", (stamp, proposal_id))
                mutation_changed = deletion_confirmed
                result_extra.update({"delete_proposal_id": proposal_id, "delete_proposal_version": proposal_version})

            cleanup_due = False
            if action == NurseryAction.FINALIZE_CLEANUP:
                deadline = parse_time(state["recycle_deadline"])
                cleanup_due = deadline is not None and deadline <= now

            target = state_after_action(
                current,
                action,
                active_pause_count=active_pause_count,
                deletion_confirmed=deletion_confirmed,
                pre_delete_state=(
                    ModuleState(state["pre_delete_state"])
                    if state["pre_delete_state"]
                    else None
                ),
                cleanup_due=cleanup_due,
                creation_confirmed=creation_confirmed,
            )
            if target == ModuleState.ACTIVE and current != ModuleState.ACTIVE:
                # Resuming or recovering from the recycle bin never erases
                # care/feed/sleep anchors, nor backfills frozen elapsed time.
                existing_clock = connection.execute(
                    "SELECT * FROM nursery_body_clocks WHERE child_id=?",
                    (operation["child_id"],),
                ).fetchone()
                resumed_clock = (
                    dict(existing_clock)
                    if existing_clock
                    else self._body_clock_payload(
                        str(operation["child_id"]),
                        stamp=stamp,
                        rule_version="body-time-disabled-v1",
                    )
                )
                resumed_clock.update(
                    {"settled_through": stamp, "frozen_at": None, "updated_at": stamp}
                )
                if existing_clock and existing_clock["frozen_at"] and existing_clock["sleep_started_at"]:
                    frozen_at = parse_time(existing_clock["frozen_at"])
                    sleep_started = parse_time(existing_clock["sleep_started_at"])
                    if frozen_at and sleep_started:
                        resumed_clock["sleep_started_at"] = iso_time(sleep_started + max(timedelta(0), now - frozen_at))
                self._save_body_clock(connection, resumed_clock)
            elif target != ModuleState.ACTIVE and current == ModuleState.ACTIVE:
                existing_clock = connection.execute(
                    "SELECT * FROM nursery_body_clocks WHERE child_id=?",
                    (operation["child_id"],),
                ).fetchone()
                frozen_clock = (
                    dict(existing_clock)
                    if existing_clock
                    else self._body_clock_payload(
                        str(operation["child_id"]),
                        stamp=stamp,
                        rule_version="body-time-disabled-v1",
                    )
                )
                frozen_clock.update(
                    {"settled_through": stamp, "frozen_at": stamp, "updated_at": stamp}
                )
                self._save_body_clock(connection, frozen_clock)
            changed = target != current or mutation_changed or body_settlement_changed
            new_version = before_version
            recycle_deadline = state["recycle_deadline"]
            pre_delete_state = state["pre_delete_state"]
            if changed:
                new_version = before_version + 1
                if target == ModuleState.DELETION_PENDING:
                    pre_delete_state = current.value
                    recycle_deadline = str(payload.get("recycle_deadline") or "").strip()
                    if not recycle_deadline:
                        recycle_deadline = iso_time(now + timedelta(days=30))
                    try:
                        parse_time(recycle_deadline)
                    except ValueError as exc:
                        raise NurseryError(
                            "INVALID_RECYCLE_DEADLINE",
                            "recycle_deadline must be an ISO timestamp",
                        ) from exc
                elif action in {
                    NurseryAction.CANCEL_DELETION,
                    NurseryAction.FINALIZE_CLEANUP,
                }:
                    pre_delete_state = None
                    recycle_deadline = None

                child_updated = connection.execute(
                    """
                    UPDATE nursery_children SET state_version=?, updated_at=?
                    WHERE child_id=? AND state_version=?
                    """,
                    (new_version, stamp, operation["child_id"], before_version),
                ).rowcount
                module_updated = connection.execute(
                    """
                    UPDATE nursery_modules SET module_state=?, state_version=?,
                        pre_delete_state=?, recycle_deadline=?, updated_at=?
                    WHERE account_id=? AND child_id=? AND state_version=?
                    """,
                    (
                        target.value,
                        new_version,
                        pre_delete_state,
                        recycle_deadline,
                        stamp,
                        operation["account_id"],
                        operation["child_id"],
                        before_version,
                    ),
                ).rowcount
                if child_updated != 1 or module_updated != 1:
                    raise NurseryError(
                        "STATE_VERSION_CHANGED",
                        "conditional state update lost its expected version",
                    )

                after_state = {
                    "module_state": target.value,
                    "state_version": new_version,
                    "stage_id": state["stage_id"],
                    "active_pause_count": active_pause_count,
                }
                if action == NurseryAction.APPLY_ANIMA_FAMILY_EVENT:
                    runtime_row = connection.execute(
                        "SELECT state_json FROM nursery_child_runtime_state WHERE child_id=?",
                        (operation["child_id"],),
                    ).fetchone()
                    after_state["runtime_state"] = self._runtime_from_row(
                        runtime_row["state_json"] if runtime_row else None
                    )
                creation_after = self._creation_snapshot(
                    connection, operation["child_id"]
                )
                if creation_after is not None:
                    after_state["creation_draft"] = creation_after
                connection.execute(
                    """
                    INSERT INTO nursery_state_snapshots (
                        operation_id, snapshot_kind, state_version,
                        state_json, created_at
                    ) VALUES (?, 'after', ?, ?, ?)
                    """,
                    (
                        operation_id,
                        new_version,
                        canonical_json(after_state),
                        stamp,
                    ),
                )

            result = {
                "action": action.value,
                "changed": changed,
                "module_state": target.value,
                "state_version": new_version,
            }
            if draft_version is not None:
                result["draft_version"] = draft_version
            result.update(result_extra)
            if action in {NurseryAction.REQUEST_DELETION, NurseryAction.CONFIRM_DELETION}:
                result["confirmation_complete"] = deletion_confirmed
            connection.execute(
                """
                UPDATE nursery_operations SET status='completed', result_json=?,
                    error_code='', error_message='', lease_owner=NULL,
                    lease_expires_at=NULL, updated_at=?, completed_at=?
                WHERE operation_id=?
                """,
                (canonical_json(result), stamp, stamp, operation_id),
            )
            connection.execute(
                "UPDATE nursery_source_events SET processing_status='applied', "
                "updated_at=? WHERE operation_id=?",
                (stamp, operation_id),
            )
            completed = connection.execute(
                "SELECT * FROM nursery_operations WHERE operation_id=?",
                (operation_id,),
            ).fetchone()
        return self._decode_operation(completed)

    def reject_operation(
        self,
        operation_id: str,
        *,
        error_code: str,
        error_message: str,
    ) -> dict[str, Any]:
        stamp = iso_time(self.clock())
        with self._write() as connection:
            connection.execute(
                """
                UPDATE nursery_operations SET status='rejected', error_code=?,
                    error_message=?, lease_owner=NULL, lease_expires_at=NULL,
                    updated_at=?, completed_at=?
                WHERE operation_id=? AND status IN ('pending', 'processing')
                """,
                (str(error_code), str(error_message)[:300], stamp, stamp, operation_id),
            )
            connection.execute(
                "UPDATE nursery_source_events SET processing_status='rejected', "
                "updated_at=? WHERE operation_id=?",
                (stamp, operation_id),
            )
            row = connection.execute(
                "SELECT * FROM nursery_operations WHERE operation_id=?",
                (operation_id,),
            ).fetchone()
        return self._decode_operation(row)

    def fail_operation_retryable(
        self,
        operation_id: str,
        *,
        error_code: str,
        error_message: str,
    ) -> dict[str, Any]:
        stamp = iso_time(self.clock())
        with self._write() as connection:
            connection.execute(
                """
                UPDATE nursery_operations SET status='failed_retryable',
                    error_code=?, error_message=?, lease_owner=NULL,
                    lease_expires_at=NULL, updated_at=?
                WHERE operation_id=? AND status='processing'
                """,
                (str(error_code), str(error_message)[:300], stamp, operation_id),
            )
            row = connection.execute(
                "SELECT * FROM nursery_operations WHERE operation_id=?",
                (operation_id,),
            ).fetchone()
        return self._decode_operation(row)

    def cancel_operation(self, operation_id: str) -> dict[str, Any]:
        stamp = iso_time(self.clock())
        with self._write() as connection:
            connection.execute(
                """
                UPDATE nursery_operations SET status='cancelled',
                    error_code='OPERATION_CANCELLED',
                    error_message='operation was cancelled', lease_owner=NULL,
                    lease_expires_at=NULL, updated_at=?, completed_at=?
                WHERE operation_id=? AND status IN ('pending', 'processing')
                """,
                (stamp, stamp, operation_id),
            )
            row = connection.execute(
                "SELECT * FROM nursery_operations WHERE operation_id=?",
                (operation_id,),
            ).fetchone()
        return self._decode_operation(row)

    def record_safety_audit(
        self,
        category: SafetyCategory | str,
        *,
        account_id: str,
        child_id: str = "",
        caregiver_id: str = "",
        operation_id: str = "",
        error_code: str = "",
        metadata: dict[str, Any] | None = None,
    ) -> None:
        with self._write() as connection:
            connection.execute(
                """
                INSERT INTO nursery_safety_audit (
                    category, account_id, child_id, caregiver_id, operation_id,
                    error_code, metadata_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    SafetyCategory(category).value,
                    str(account_id),
                    str(child_id) or None,
                    str(caregiver_id) or None,
                    str(operation_id) or None,
                    str(error_code),
                    canonical_json(sanitize_audit_metadata(metadata)),
                    iso_time(self.clock()),
                ),
            )

    def record_health_evaluation(self, record: dict[str, Any]) -> dict[str, Any]:
        stamp = iso_time(self.clock())
        with self._write() as connection:
            existing = connection.execute(
                "SELECT * FROM nursery_health_evaluations WHERE evaluation_key=?",
                (record["evaluation_key"],),
            ).fetchone()
            if existing:
                result = dict(existing)
                result["replayed"] = True
                return result
            connection.execute(
                """
                INSERT INTO nursery_health_evaluations (
                    evaluation_key, account_id, child_id, operation_id,
                    rule_version, trigger_type, condition_hash, source_type,
                    source_id, source_version, child_state_version, outcome,
                    replayed, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?)
                """,
                (
                    record["evaluation_key"],
                    record["account_id"],
                    record["child_id"],
                    record.get("operation_id"),
                    record["rule_version"],
                    record["trigger_type"],
                    record["condition_hash"],
                    record["source_type"],
                    record["source_id"],
                    record["source_version"],
                    int(record["child_state_version"]),
                    HealthOutcome(record["outcome"]).value,
                    stamp,
                ),
            )
            row = connection.execute(
                "SELECT * FROM nursery_health_evaluations WHERE evaluation_key=?",
                (record["evaluation_key"],),
            ).fetchone()
        result = dict(row)
        result["replayed"] = bool(result["replayed"])
        return result

    def get_health_evaluation(self, evaluation_key: str) -> dict[str, Any] | None:
        with self._read() as connection:
            row = connection.execute(
                "SELECT * FROM nursery_health_evaluations WHERE evaluation_key=?",
                (str(evaluation_key),),
            ).fetchone()
        if not row:
            return None
        result = dict(row)
        result["replayed"] = True
        return result

    def health_guard_state(
        self,
        child_id: str,
        *,
        since: datetime | None = None,
    ) -> dict[str, Any]:
        with self._read() as connection:
            if since is None:
                count = connection.execute(
                    "SELECT COUNT(*) FROM nursery_health_evaluations WHERE child_id=?",
                    (str(child_id),),
                ).fetchone()[0]
            else:
                count = connection.execute(
                    """
                    SELECT COUNT(*) FROM nursery_health_evaluations
                    WHERE child_id=? AND created_at>=?
                    """,
                    (str(child_id), iso_time(since)),
                ).fetchone()[0]
            latest = connection.execute(
                """
                SELECT outcome, created_at FROM nursery_health_evaluations
                WHERE child_id=? ORDER BY evaluation_id DESC LIMIT 1
                """,
                (str(child_id),),
            ).fetchone()
        return {
            "count": int(count),
            "latest_outcome": str(latest["outcome"]) if latest else "",
            "latest_at": str(latest["created_at"]) if latest else "",
        }

    def count_rows(self, table: str) -> int:
        if table not in EXPECTED_TABLES:
            raise ValueError("table is not in the nursery schema whitelist")
        with self._read() as connection:
            return int(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])

    def snapshots_for(self, operation_id: str) -> list[dict[str, Any]]:
        with self._read() as connection:
            rows = connection.execute(
                """
                SELECT snapshot_kind, state_version, state_json
                FROM nursery_state_snapshots WHERE operation_id=?
                ORDER BY snapshot_id
                """,
                (operation_id,),
            ).fetchall()
        return [
            {
                "snapshot_kind": row["snapshot_kind"],
                "state_version": int(row["state_version"]),
                "state": json.loads(row["state_json"]),
            }
            for row in rows
        ]

    def source_for(self, operation_id: str) -> dict[str, Any] | None:
        with self._read() as connection:
            row = connection.execute(
                "SELECT * FROM nursery_source_events WHERE operation_id=?",
                (operation_id,),
            ).fetchone()
        return dict(row) if row else None

    def mark_source_revoked(
        self,
        *,
        account_id: str,
        child_id: str,
        source_type: str,
        source_id: str,
        source_version: str,
    ) -> dict[str, Any]:
        """Correct source-derived child views without deleting their audit trail."""

        stamp = iso_time(self.clock())
        with self._write() as connection:
            row = connection.execute(
                """
                SELECT * FROM nursery_source_events
                WHERE account_id=? AND child_id=? AND source_type=?
                    AND source_id=? AND source_version=?
                """,
                (
                    str(account_id),
                    str(child_id),
                    str(source_type),
                    str(source_id),
                    str(source_version),
                ),
            ).fetchone()
            if not row:
                raise NurseryError("SOURCE_NOT_FOUND", "source event was not found")
            connection.execute(
                """
                UPDATE nursery_source_events SET processing_status='revoked',
                    revoked_at=COALESCE(revoked_at, ?), updated_at=?
                WHERE source_event_key=?
                """,
                (stamp, stamp, int(row["source_event_key"])),
            )
            experience_rows = connection.execute(
                """
                SELECT s.experience_id FROM nursery_experience_sources s
                JOIN nursery_experiences e ON e.experience_id=s.experience_id
                WHERE e.child_id=? AND s.source_type=? AND s.source_id=?
                    AND s.source_version=?
                """,
                (
                    str(child_id),
                    str(source_type),
                    str(source_id),
                    str(source_version),
                ),
            ).fetchall()
            experience_ids = [str(item["experience_id"]) for item in experience_rows]
            if experience_ids:
                placeholders = ",".join("?" for _ in experience_ids)
                connection.execute(
                    f"""
                    UPDATE nursery_experience_sources SET source_validity='revoked'
                    WHERE experience_id IN ({placeholders})
                    """,
                    experience_ids,
                )
                connection.execute(
                    f"""
                    UPDATE nursery_experiences
                    SET resolution='corrected', updated_at=?
                    WHERE experience_id IN ({placeholders})
                    """,
                    (stamp, *experience_ids),
                )
                connection.execute(
                    f"""
                    UPDATE nursery_experience_ripples
                    SET active=0, current_ripple=NULL, recalculation_version='source-revoked-v1', updated_at=?
                    WHERE experience_id IN ({placeholders})
                    """,
                    (stamp, *experience_ids),
                )
            connection.execute(
                """
                UPDATE nursery_anima_child_contexts SET source_validity='revoked'
                WHERE child_id=? AND source_key=? AND source_version=?
                """,
                (str(row["child_id"]), str(source_id), str(source_version)),
            )
            updated = connection.execute(
                "SELECT * FROM nursery_source_events WHERE source_event_key=?",
                (int(row["source_event_key"]),),
            ).fetchone()
        return dict(updated)

    def supersede_source(
        self,
        *,
        account_id: str,
        child_id: str,
        source_type: str,
        source_id: str,
        source_version: str,
        superseded_by_source_version: str,
    ) -> dict[str, Any]:
        """Correct one previous source version before its replacement is applied."""

        replacement = str(superseded_by_source_version).strip()
        if not replacement:
            raise NurseryError("INVALID_SOURCE", "replacement source version is required")
        stamp = iso_time(self.clock())
        with self._write() as connection:
            row = connection.execute(
                """
                SELECT * FROM nursery_source_events
                WHERE account_id=? AND child_id=? AND source_type=?
                    AND source_id=? AND source_version=?
                """,
                (
                    str(account_id),
                    str(child_id),
                    str(source_type),
                    str(source_id),
                    str(source_version),
                ),
            ).fetchone()
            if not row:
                raise NurseryError("SOURCE_NOT_FOUND", "source event was not found")
            connection.execute(
                """
                UPDATE nursery_source_events SET processing_status='superseded', updated_at=?
                WHERE source_event_key=?
                """,
                (stamp, int(row["source_event_key"])),
            )
            experience_rows = connection.execute(
                """
                SELECT s.experience_id FROM nursery_experience_sources s
                JOIN nursery_experiences e ON e.experience_id=s.experience_id
                WHERE e.child_id=? AND s.source_type=? AND s.source_id=?
                    AND s.source_version=?
                """,
                (
                    str(child_id),
                    str(source_type),
                    str(source_id),
                    str(source_version),
                ),
            ).fetchall()
            experience_ids = [str(item["experience_id"]) for item in experience_rows]
            if experience_ids:
                placeholders = ",".join("?" for _ in experience_ids)
                connection.execute(
                    f"UPDATE nursery_experience_sources SET source_validity='modified' WHERE experience_id IN ({placeholders})",
                    experience_ids,
                )
                connection.execute(
                    f"UPDATE nursery_experiences SET resolution='corrected', updated_at=? WHERE experience_id IN ({placeholders})",
                    (stamp, *experience_ids),
                )
                connection.execute(
                    f"""
                    UPDATE nursery_experience_ripples
                    SET active=0, current_ripple=NULL, recalculation_version='source-superseded-v1', updated_at=?
                    WHERE experience_id IN ({placeholders})
                    """,
                    (stamp, *experience_ids),
                )
            connection.execute(
                """
                UPDATE nursery_anima_child_contexts SET source_validity='superseded'
                WHERE child_id=? AND source_key=? AND source_version=?
                """,
                (str(row["child_id"]), str(source_id), str(source_version)),
            )
            updated = connection.execute(
                "SELECT * FROM nursery_source_events WHERE source_event_key=?",
                (int(row["source_event_key"]),),
            ).fetchone()
        return dict(updated)

    def audit_rows(self) -> list[dict[str, Any]]:
        with self._read() as connection:
            rows = connection.execute(
                "SELECT * FROM nursery_safety_audit ORDER BY audit_id"
            ).fetchall()
        return [dict(row) for row in rows]

    @staticmethod
    def _json_value(value: Any, fallback: Any) -> Any:
        try:
            return json.loads(str(value)) if value not in {None, ""} else fallback
        except (TypeError, ValueError):
            return fallback

    def get_child_feature_view(
        self,
        *,
        actor: ActorContext,
        child_id: str,
        detail: str = "summary",
        query: str = "",
        cursor: str | None = None,
        limit: int = 50,
    ) -> dict[str, Any]:
        """Read one role-trimmed feature view without creating an operation."""

        selected = str(detail or "summary").strip().lower()
        if selected not in {
            "summary",
            "events",
            "world",
            "growth",
            "care",
            "settings",
            "conversations",
            "conversation_user",
            "conversation_external",
            "family",
        }:
            raise NurseryError("INVALID_STATUS_DETAIL", "unsupported child status detail")
        page_size = max(1, min(100, int(limit)))
        try:
            offset = max(0, int(cursor or 0))
        except (TypeError, ValueError) as exc:
            raise NurseryError("INVALID_CURSOR", "cursor must be a non-negative integer") from exc
        search = " ".join(str(query or "").split()).lower()
        with self._read() as connection:
            self._assert_actor(connection, actor)
            state = connection.execute(
                """
                SELECT m.account_id, m.child_id, m.module_state, m.state_version,
                       m.pre_delete_state, m.recycle_deadline, c.stage_id,
                       (SELECT COUNT(*) FROM nursery_pause_markers p
                        WHERE p.child_id=m.child_id AND p.released_at IS NULL)
                        AS active_pause_count
                FROM nursery_modules m JOIN nursery_children c ON c.child_id=m.child_id
                WHERE m.account_id=? AND m.child_id=?
                """,
                (actor.account_id, str(child_id)),
            ).fetchone()
            if not state:
                raise NurseryError("NURSERY_NOT_FOUND", "nursery child was not found")

            identity_row = connection.execute(
                "SELECT child_id, sex_status, official_name, nickname FROM nursery_child_identity WHERE child_id=?",
                (str(child_id),),
            ).fetchone()
            runtime_row = connection.execute(
                "SELECT state_json, state_updated_at FROM nursery_child_runtime_state WHERE child_id=?",
                (str(child_id),),
            ).fetchone()
            room = connection.execute(
                "SELECT * FROM nursery_rooms WHERE child_id=?", (str(child_id),)
            ).fetchone()
            presentation = connection.execute(
                "SELECT * FROM nursery_child_presentations WHERE child_id=?", (str(child_id),)
            ).fetchone()
            body_clock = connection.execute(
                "SELECT * FROM nursery_body_clocks WHERE child_id=?", (str(child_id),)
            ).fetchone()
            identity = dict(identity_row) if identity_row else {}
            runtime = self._runtime_from_row(runtime_row["state_json"] if runtime_row else None)
            latest_family_event = connection.execute(
                """
                SELECT e.experience_id, e.occurred_at, e.age_appropriate_summary,
                       e.child_reaction, r.current_ripple, r.updated_at
                FROM nursery_experiences e
                JOIN nursery_experience_ripples r ON r.experience_id=e.experience_id
                JOIN nursery_experience_sources s ON s.experience_id=e.experience_id
                WHERE e.child_id=? AND e.category='family' AND e.resolution='unresolved'
                  AND r.active=1 AND s.source_validity='valid'
                ORDER BY r.updated_at DESC, e.experience_id DESC LIMIT 1
                """,
                (str(child_id),),
            ).fetchone()
            emotion = dict(DEFAULT_CHILD_RUNTIME_STATE["emotion"])
            if isinstance(runtime.get("emotion"), dict):
                emotion.update(
                    {
                        key: value
                        for key, value in runtime["emotion"].items()
                        if key in emotion
                    }
                )
            emotion["primary"] = " ".join(str(emotion.get("primary") or "安稳").split())[:24] or "安稳"
            secondary = emotion.get("secondary", [])
            if not isinstance(secondary, list):
                secondary = []
            emotion["secondary"] = [
                " ".join(str(item).split())[:24]
                for item in secondary
                if " ".join(str(item).split())[:24]
            ][:3]
            for field, default in (("valence", 0.35), ("arousal", 0.3), ("safety", 0.8)):
                try:
                    emotion[field] = round(float(emotion.get(field, default)), 3)
                except (TypeError, ValueError):
                    emotion[field] = default
            emotion["cause"] = " ".join(str(emotion.get("cause") or "").split())[:160]
            recent_event = None
            if latest_family_event:
                recent_event = {
                    "experience_id": str(latest_family_event["experience_id"]),
                    "occurred_at": str(latest_family_event["occurred_at"]),
                    "summary": str(latest_family_event["age_appropriate_summary"]),
                    "child_reaction": str(latest_family_event["child_reaction"]),
                    "current_ripple": str(latest_family_event["current_ripple"] or ""),
                    "updated_at": str(latest_family_event["updated_at"]),
                }
                runtime_updated_at = str(runtime_row["state_updated_at"] or "") if runtime_row else ""
                if recent_event["updated_at"] > runtime_updated_at:
                    # A family event changes the child's visible present without
                    # inventing hunger, illness or a permanent personality fact.
                    # A later direct interaction naturally becomes the new present.
                    emotion["primary"] = "在意"
                    emotion["secondary"] = []
                    emotion["cause"] = (
                        recent_event["current_ripple"]
                        or recent_event["child_reaction"]
                    )[:160]
            result: dict[str, Any] = {
                "module_state": str(state["module_state"]),
                "child_id": str(child_id),
                "state_version": int(state["state_version"]),
            }
            if state["module_state"] not in {ModuleState.ACTIVE.value, ModuleState.PAUSED.value}:
                if state["module_state"] == ModuleState.DELETION_PENDING.value:
                    result.update(
                        {
                            "recycle_deadline": state["recycle_deadline"],
                            "restore_state": state["pre_delete_state"],
                        }
                    )
                return result

            result.update(
                {
                    "identity": {
                        "display_name": str(identity.get("nickname") or identity.get("official_name") or "孩子"),
                        "official_name": identity.get("official_name"),
                        "nickname": identity.get("nickname"),
                        "sex_status": str(identity.get("sex_status") or "neutral"),
                        "stage": str(state["stage_id"]),
                    },
                    "location": {
                        "area_id": None,
                        "label": str(room["name"] if room else "房间"),
                        "current_action": str(runtime.get("last_intent") or "none"),
                    },
                    "current_state": {
                        "emotions": [emotion["primary"], *emotion["secondary"]],
                        "emotion": emotion,
                        "valence": emotion["valence"],
                        "arousal": emotion["arousal"],
                        "safety": emotion["safety"],
                        "body_needs": [],
                        "interaction_willingness": "ready"
                        if float(runtime.get("connection", 0.0)) >= 0.4
                        else "quiet",
                        "recent_family_event": recent_event,
                    },
                    "time_context": {
                        "last_fed_at": body_clock["last_fed_at"] if body_clock else None,
                        "last_hydrated_at": body_clock["last_hydrated_at"] if body_clock else None,
                        "sleep_started_at": body_clock["sleep_started_at"] if body_clock else None,
                        "last_woke_at": body_clock["last_woke_at"] if body_clock else None,
                        "last_care_at": body_clock["last_care_at"] if body_clock else None,
                        "evaluated_at": iso_time(self.clock()),
                        "next_change_hint": None,
                        "settlement_due": False,
                    },
                    "saved_health_observations": [],
                    "presentation": (
                        {
                            "scene_key": presentation["scene_key"],
                            "character_variant": presentation["character_variant"],
                            "time_tone": presentation["time_tone"],
                            "scene_summary": presentation["scene_summary"],
                            "suggested_interactions": self._json_value(presentation["suggestions_json"], [])[:2],
                            "first_room_arrival": {
                                "event_id": presentation["first_arrival_event_id"],
                                "status": presentation["first_arrival_status"],
                                "age_appropriate_reaction": presentation["first_arrival_reaction"],
                            },
                        }
                        if presentation
                        else {
                            "scene_key": None,
                            "character_variant": None,
                            "time_tone": None,
                            "scene_summary": None,
                            "suggested_interactions": [],
                            "first_room_arrival": {"event_id": None, "status": "none", "age_appropriate_reaction": None},
                        }
                    ),
                }
            )

            health_rows = connection.execute(
                """
                SELECT h.health_event_id, h.status, h.non_diagnostic_summary,
                       h.started_at, o.observation_id, o.observed_at
                FROM nursery_health_events h
                LEFT JOIN nursery_health_observations o ON o.health_event_id=h.health_event_id
                WHERE h.child_id=? AND h.status IN ('monitoring','caring','improving')
                ORDER BY COALESCE(o.observed_at, h.started_at) DESC
                """,
                (str(child_id),),
            ).fetchall()
            result["saved_health_observations"] = [
                {
                    "observation_id": str(row["observation_id"] or row["health_event_id"]),
                    "status": str(row["status"]),
                    "summary": str(row["non_diagnostic_summary"]),
                    "observed_at": str(row["observed_at"] or row["started_at"]),
                    "needs_attention": row["status"] in {"monitoring", "caring"},
                }
                for row in health_rows
            ]

            if selected in {"conversations", "conversation_user", "conversation_external"}:
                stamp = iso_time(self.clock())
                if selected == "conversation_user":
                    if actor.role != CaregiverRole.USER_GUARDIAN:
                        raise NurseryError("CONVERSATION_NOT_AVAILABLE", "this conversation belongs to the user guardian")
                    channels = ("user_child", "legacy")
                    caregiver_filter: tuple[str, ...] = ()
                elif selected == "conversation_external":
                    if actor.role == CaregiverRole.USER_GUARDIAN:
                        channels = ("external_ai_child",)
                        caregiver_filter = ()
                    elif actor.role == CaregiverRole.EXTERNAL_AI_GUARDIAN:
                        channels = ("external_ai_child",)
                        caregiver_filter = (actor.caregiver_id,)
                    else:
                        raise NurseryError("CONVERSATION_NOT_AVAILABLE", "this conversation is not available to this caregiver")
                elif actor.role == CaregiverRole.USER_GUARDIAN:
                    channels = ("user_child", "legacy")
                    caregiver_filter = ()
                elif actor.role == CaregiverRole.EXTERNAL_AI_GUARDIAN:
                    channels = ("external_ai_child",)
                    caregiver_filter = (actor.caregiver_id,)
                else:
                    channels = ("shared_family",)
                    caregiver_filter = (actor.caregiver_id,)
                channel_marks = ", ".join("?" for _ in channels)
                extra_filter = " AND e.caregiver_id=?" if caregiver_filter else ""
                params: tuple[Any, ...] = (
                    str(child_id),
                    *channels,
                    stamp,
                    *caregiver_filter,
                )
                rows = [
                    dict(row)
                    for row in connection.execute(
                        f"""
                        SELECT e.event_id, e.caregiver_id, e.conversation_channel,
                               e.caregiver_message, e.child_reply, e.intent,
                               e.created_at, e.expires_at,
                               p.display_name AS caregiver_display_name
                        FROM nursery_child_short_events e
                        LEFT JOIN nursery_caregiver_profiles p
                          ON p.caregiver_id=e.caregiver_id AND p.child_id=e.child_id
                        WHERE e.child_id=? AND e.conversation_channel IN ({channel_marks})
                          AND e.event_kind='interaction' AND e.expires_at>?{extra_filter}
                        ORDER BY e.created_at DESC, e.event_id DESC
                        """,
                        params,
                    ).fetchall()
                ]
                if search:
                    rows = [
                        row
                        for row in rows
                        if search
                        in " ".join(
                            str(row.get(key) or "").lower()
                            for key in (
                                "caregiver_display_name",
                                "caregiver_message",
                                "child_reply",
                                "intent",
                            )
                        )
                    ]
                page = rows[offset : offset + page_size]
                result["conversation_view"] = {
                    "turns": [
                        {
                            "event_id": row["event_id"],
                            "caregiver_id": row["caregiver_id"],
                            "conversation_channel": row["conversation_channel"],
                            "caregiver_display_name": row.get("caregiver_display_name")
                            or "一位养育者",
                            "message": row["caregiver_message"],
                            "reply": row["child_reply"],
                            "intent": row["intent"],
                            "created_at": row["created_at"],
                            "expires_at": row["expires_at"],
                        }
                        for row in reversed(page)
                    ],
                    "next_cursor": str(offset + page_size)
                    if offset + page_size < len(rows)
                    else None,
                    "retention_days": 30,
                    "channel": channels[0] if len(channels) == 1 else "user_and_legacy",
                }
            elif selected == "events":
                rows = [dict(row) for row in connection.execute(
                    """
                    SELECT e.*, s.source_id, s.source_version, s.source_validity,
                           r.current_ripple, r.active
                    FROM nursery_experiences e
                    LEFT JOIN nursery_experience_ripples r ON r.experience_id=e.experience_id
                    LEFT JOIN nursery_experience_sources s ON s.experience_id=e.experience_id
                    WHERE e.child_id=? ORDER BY e.occurred_at DESC, e.experience_id DESC
                    """,
                    (str(child_id),),
                ).fetchall()]
                if search:
                    rows = [row for row in rows if search in " ".join(str(row.get(key) or "").lower() for key in ("category", "age_appropriate_summary", "child_reaction", "occurred_at"))]
                page = rows[offset : offset + page_size]
                timeline = [
                    {
                        "experience_id": row["experience_id"],
                        "occurred_at": row["occurred_at"],
                        "category": row["category"],
                        "source_event_id": row.get("source_id"),
                        "source_event_version": row.get("source_version"),
                        "age_appropriate_summary": row["age_appropriate_summary"],
                        "child_reaction": row["child_reaction"],
                        "current_ripple": row.get("current_ripple"),
                        "resolution": row["resolution"],
                        "source_validity": row.get("source_validity") or "valid",
                    }
                    for row in page
                ]
                result["event_view"] = {
                    "active_ripples": [item for item in timeline if next((r for r in page if r["experience_id"] == item["experience_id"]), {}).get("active")][:3],
                    "timeline": timeline,
                    "next_cursor": str(offset + page_size) if offset + page_size < len(rows) else None,
                }
            elif selected == "world":
                areas = [dict(row) for row in connection.execute("SELECT * FROM nursery_areas WHERE child_id=? ORDER BY created_at, area_id", (str(child_id),)).fetchall()]
                items = [dict(row) for row in connection.execute("SELECT * FROM nursery_items WHERE child_id=? AND removed_at IS NULL ORDER BY COALESCE(last_interacted_at, created_at) DESC", (str(child_id),)).fetchall()]
                profile_names = {row["caregiver_id"]: row["display_name"] for row in connection.execute(
                    "SELECT caregiver_id,display_name FROM nursery_caregiver_profiles WHERE child_id=?", (str(child_id),)
                ).fetchall()}
                if search:
                    areas = [row for row in areas if search in f"{row['label']} {row['description'] or ''}".lower()]
                    items = [row for row in items if search in f"{row['name']} {row['description'] or ''} {row['current_state'] or ''}".lower()]
                less_used_before = self.clock() - timedelta(days=30)
                item_view = [
                    {
                        "item_id": row["item_id"], "name": row["name"], "emoji": row["emoji"],
                        "description": row["description"], "location_id": row["area_id"],
                        "current_state": row["current_state"],
                        "last_interacted_at": row["last_interacted_at"],
                        "artwork_data_url": self._json_value(row["facts_json"], {}).get("artwork_data_url"),
                        "recorded_by": [
                            {"caregiver_id": identity, "display_name": profile_names.get(identity) or "一位养育者"}
                            for identity in self._json_value(row["facts_json"], {}).get("recorded_by", [])
                            if isinstance(identity, str)
                        ],
                        "display_group": "less_used" if (parse_time(row["last_interacted_at"]) or parse_time(row["created_at"])) < less_used_before else "active",
                    }
                    for row in items
                ]
                result["world_view"] = {
                    "room": ({"room_id": room["room_id"], "name": room["name"], "atmosphere": room["atmosphere"]} if room else None),
                    "in_use": [item for item in item_view if item["current_state"]][:10],
                    "areas": [{"area_id": row["area_id"], "category": row["category"], "label": row["label"], "description": row["description"]} for row in areas],
                    "items": item_view[:page_size], "next_cursor": None,
                }
            elif selected == "family":
                cutoff = iso_time(self.clock() - timedelta(days=30))
                threads = connection.execute(
                    """SELECT t.*, p.display_name AS updated_by_name FROM nursery_family_threads t
                    LEFT JOIN nursery_caregiver_profiles p ON p.caregiver_id=t.updated_by AND p.child_id=t.child_id
                    WHERE t.child_id=? AND t.updated_at>? ORDER BY t.updated_at DESC,t.thread_id LIMIT ?""",
                    (str(child_id), cutoff, page_size),
                ).fetchall()
                events = connection.execute(
                    """SELECT e.event_id,e.thread_id,e.caregiver_id,e.kind,e.summary,e.created_at,
                    p.display_name FROM nursery_family_events e LEFT JOIN nursery_caregiver_profiles p
                    ON p.caregiver_id=e.caregiver_id AND p.child_id=e.child_id
                    WHERE e.child_id=? AND e.created_at>? ORDER BY e.created_at DESC,e.event_id LIMIT ?""",
                    (str(child_id), cutoff, page_size),
                ).fetchall()
                relation = connection.execute(
                    "SELECT trust,familiarity,closeness,repair,updated_at FROM nursery_relationships WHERE child_id=? AND caregiver_id=? AND updated_at>?",
                    (str(child_id), actor.caregiver_id, cutoff),
                ).fetchone()
                result["family_view"] = {
                    "threads": [{**dict(row), "can_edit": row["kind"] != "promise" or row["created_by"] == actor.caregiver_id,
                        "current_session_over": row["kind"] == "activity" and row["status"] == "active"
                            and parse_time(row["updated_at"]) <= self.clock() - timedelta(hours=2)
                        } for row in threads],
                    "events": [dict(row) for row in events],
                    "my_relationship": dict(relation) if relation else None,
                }
            elif selected == "growth":
                observations = [dict(row) for row in connection.execute("SELECT * FROM nursery_growth_observations WHERE child_id=? AND valid=1 AND created_at>? ORDER BY observed_at DESC", (str(child_id), iso_time(self.clock()-timedelta(days=30)))).fetchall()]
                proposals = [dict(row) for row in connection.execute("SELECT * FROM nursery_proposals WHERE child_id=? AND proposal_type='stage' ORDER BY updated_at DESC", (str(child_id),)).fetchall()]
                confirmations = [dict(row) for row in connection.execute(
                    """
                    SELECT n.subject_id, n.subject_version, n.caregiver_id,
                           n.confirmation_status, n.updated_at, p.display_name, c.role
                    FROM nursery_confirmations n
                    JOIN nursery_proposals q ON q.proposal_id=n.subject_id
                    JOIN nursery_caregivers c ON c.caregiver_id=n.caregiver_id
                    JOIN nursery_caregiver_profiles p
                      ON p.caregiver_id=c.caregiver_id AND p.child_id=q.child_id
                    WHERE q.child_id=? AND q.proposal_type='stage'
                      AND n.subject_type='stage_change'
                      AND c.permission_status='active' AND p.founding_guardian=1
                    ORDER BY n.updated_at, n.caregiver_id
                    """, (str(child_id),)
                ).fetchall()]
                result["growth_view"] = {
                    "stage": str(state["stage_id"]),
                    "observations": [
                        {
                            "observation_id": row["observation_id"],
                            "session_id": row["session_id"],
                            "ability_ids": self._json_value(row["ability_ids_json"], []),
                            "observed_behavior": row["observed_behavior"],
                            "assistance": row["assistance"],
                            "observed_at": row["observed_at"],
                        }
                        for row in observations
                    ],
                    "proposals": [
                        {
                            "proposal_id": row["proposal_id"],
                            "proposal_version": int(row["proposal_version"]),
                            "status": row["status"],
                            "proposed": self._json_value(row["proposed_json"], {}),
                            "can_withdraw": row["status"] == "open" and row["proposed_by"] == actor.caregiver_id,
                            "confirmed_by_me": any(
                                item["subject_id"] == row["proposal_id"]
                                and str(item["subject_version"]) == str(row["proposal_version"])
                                and item["caregiver_id"] == actor.caregiver_id
                                and item["confirmation_status"] == "confirmed"
                                for item in confirmations
                            ),
                            "confirmations": [
                                {key: item[key] for key in (
                                    "caregiver_id", "display_name", "role",
                                    "confirmation_status", "updated_at",
                                )}
                                for item in confirmations
                                if item["subject_id"] == row["proposal_id"]
                                and str(item["subject_version"]) == str(row["proposal_version"])
                            ],
                            "created_at": row["created_at"],
                            "updated_at": row["updated_at"],
                        }
                        for row in proposals
                    ],
                }
            elif selected == "care":
                events = [dict(row) for row in connection.execute("SELECT * FROM nursery_health_events WHERE child_id=? ORDER BY started_at DESC", (str(child_id),)).fetchall()]
                plans = [dict(row) for row in connection.execute("SELECT * FROM nursery_care_plans WHERE child_id=? ORDER BY created_at DESC", (str(child_id),)).fetchall()]
                observations = [dict(row) for row in connection.execute(
                    """
                    SELECT o.*, p.display_name
                    FROM nursery_health_observations o
                    LEFT JOIN nursery_caregiver_profiles p
                      ON p.caregiver_id=o.caregiver_id AND p.child_id=o.child_id
                    WHERE o.child_id=? ORDER BY o.observed_at DESC, o.observation_id DESC
                    """,
                    (str(child_id),),
                ).fetchall()]
                executions = [dict(row) for row in connection.execute(
                    """
                    SELECT e.*, p.display_name
                    FROM nursery_care_executions e
                    LEFT JOIN nursery_caregiver_profiles p
                      ON p.caregiver_id=e.caregiver_id AND p.child_id=e.child_id
                    WHERE e.child_id=? ORDER BY e.executed_at DESC, e.execution_id DESC
                    """,
                    (str(child_id),),
                ).fetchall()]
                shared_care = [dict(row) for row in connection.execute(
                    """
                    SELECT e.*, p.display_name AS caregiver_display_name
                    FROM nursery_shared_care_events e
                    LEFT JOIN nursery_caregiver_profiles p
                      ON p.caregiver_id=e.caregiver_id AND p.child_id=e.child_id
                    WHERE e.child_id=? AND e.created_at>?
                    ORDER BY e.created_at DESC, e.care_event_id DESC
                    LIMIT ?
                    """,
                    (str(child_id), iso_time(self.clock() - timedelta(days=30)), page_size),
                ).fetchall()]
                result["care_view"] = {
                    "shared_care": [
                        {
                            "care_event_id": row["care_event_id"],
                            "correction": self._json_value(row["correction_json"], None),
                            "can_correct": not row["correction_json"] and row["category"] != "health" and (
                                row["caregiver_id"] == actor.caregiver_id or actor.role == CaregiverRole.USER_GUARDIAN
                            ),
                            "category": row["category"],
                            "object_name": row["object_name"],
                            "summary": row["summary"],
                            "world_item_id": row["world_item_id"],
                            "caregiver_id": row["caregiver_id"],
                            "caregiver_display_name": row["caregiver_display_name"] or "一位养育者",
                            "conversation_channel": row["conversation_channel"],
                            "state_before": self._json_value(row["state_before_json"], {}),
                            "state_after": self._json_value(row["state_after_json"], {}),
                            "created_at": row["created_at"],
                        }
                        for row in reversed(shared_care)
                    ],
                    "active_events": [
                        {
                            "health_event_id": row["health_event_id"],
                            "status": row["status"],
                            "summary": row["non_diagnostic_summary"],
                            "observed_effects": self._json_value(row["observed_effects_json"], {}),
                            "condition_summary": self._json_value(row["condition_summary_json"], []),
                            "observations": [
                                {
                                    "observation_id": item["observation_id"],
                                    "observed_behavior": item["observed_behavior"],
                                    "context_summary": item["context_summary"],
                                    "observed_at": item["observed_at"],
                                    "observed_by": item["caregiver_id"],
                                    "observed_by_display_name": item["display_name"],
                                }
                                for item in observations
                                if item["health_event_id"] == row["health_event_id"]
                            ],
                            "started_at": row["started_at"],
                            "updated_at": row["updated_at"],
                        }
                        for row in events
                        if row["status"] not in {"recovered", "corrected"}
                    ],
                    "confirmed_plans": [
                        {
                            "care_plan_id": row["care_plan_id"],
                            "health_event_id": row["health_event_id"],
                            "plan_version": int(row["plan_version"]),
                            "summary": row["user_confirmed_summary"],
                            "status": row["status"],
                            "confirmed_by_user_at": row["confirmed_by_user_at"],
                        }
                        for row in plans
                        if row["status"] == "confirmed"
                    ],
                    "executions": [
                        {
                            "execution_id": row["execution_id"],
                            "care_plan_id": row["care_plan_id"],
                            "care_plan_version": int(row["care_plan_version"]),
                            "health_event_id": row["health_event_id"],
                            "executed_at": row["executed_at"],
                            "executed_by": row["caregiver_id"],
                            "executed_by_display_name": row["display_name"],
                        }
                        for row in executions
                    ],
                    "history": [
                        {
                            "health_event_id": row["health_event_id"],
                            "status": row["status"],
                            "summary": row["non_diagnostic_summary"],
                            "started_at": row["started_at"],
                            "recovered_at": row["recovered_at"],
                        }
                        for row in events
                        if row["status"] in {"recovered", "corrected"}
                    ],
                    "next_cursor": None,
                }
            elif selected == "settings":
                model = connection.execute("SELECT provider_id, model_name, connection_status, tested_at FROM nursery_model_configs WHERE account_id=?", (actor.account_id,)).fetchone()
                linkage = connection.execute("SELECT child_snapshot_reference_authorized FROM nursery_linkage_preferences WHERE child_id=?", (str(child_id),)).fetchone()
                caregivers = connection.execute("""
                    SELECT c.caregiver_id, c.role, p.display_name, p.founding_guardian
                    FROM nursery_caregivers c LEFT JOIN nursery_caregiver_profiles p ON p.caregiver_id=c.caregiver_id
                    WHERE c.account_id=? AND c.permission_status='active' ORDER BY c.role
                    """, (actor.account_id,)).fetchall()
                delete_proposal = connection.execute("SELECT proposal_id, proposal_version, status, proposed_json FROM nursery_proposals WHERE child_id=? AND proposal_type='delete' ORDER BY updated_at DESC LIMIT 1", (str(child_id),)).fetchone()
                result["settings_view"] = {
                    "caregivers": [{"role": row["role"], "display_name": row["display_name"], "founding_guardian": bool(row["founding_guardian"])} for row in caregivers],
                    "model": (dict(model) if model else {"connection_status": "unavailable"}),
                    "pause": {"active_pause_count": int(state["active_pause_count"])},
                    "delete_proposal": ({**dict(delete_proposal), "proposed": self._json_value(delete_proposal["proposed_json"], {})} if delete_proposal else None),
                    "anima_linkage": {
                        "immediate_family_summary": "active" if state["module_state"] == ModuleState.ACTIVE.value else "inactive_due_to_module_state",
                        "child_snapshot_reference_authorized": bool(linkage and linkage["child_snapshot_reference_authorized"]),
                        "startup_summary_eligible": state["module_state"] == ModuleState.ACTIVE.value,
                    },
                }
            return result
