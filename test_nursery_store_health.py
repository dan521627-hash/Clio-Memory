from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from nursery.body_state import BodyTimeRule, effective_body_view
from nursery.coordinator import NurseryCoordinator
from nursery.health_contract import (
    FixedHealthRule,
    HealthEvaluationService,
    HealthGuardConfig,
)
from nursery.models import (
    HealthOutcome,
    HealthTrigger,
    ModuleState,
    ActorContext,
    AuthSource,
    CaregiverRole,
    NurseryAction,
    NurseryCommand,
    NurseryError,
    OperationStatus,
    SafetyCategory,
    SourceReference,
    SourceType,
)
from nursery.queries import NurseryQueryService
from nursery.schemas import (
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
)
from nursery.store import NurseryStore


class MutableClock:
    def __init__(self):
        self.value = datetime(2026, 8, 30, 12, 0, tzinfo=timezone.utc)

    def __call__(self):
        return self.value

    def advance(self, **kwargs):
        self.value += timedelta(**kwargs)


def source(number: int = 1) -> SourceReference:
    return SourceReference.build(
        source_type=SourceType.TEST_FIXTURE,
        source_id=f"health-event-{number}",
        source_version="v1",
        specification_ref="nursery-stage1-test",
    )


class NurseryStoreMigrationTests(unittest.TestCase):
    def test_empty_database_initializes_idempotently(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "nursery.sqlite3"
            first = NurseryStore(path)
            second = NurseryStore(path)
            self.assertEqual(first.schema_version(), 11)
            self.assertEqual(second.schema_version(), 11)
            with closing(sqlite3.connect(path)) as connection:
                tables = {
                    row[0]
                    for row in connection.execute(
                        "SELECT name FROM sqlite_master WHERE type='table'"
                    )
                }
                migration_count = connection.execute(
                    "SELECT COUNT(*) FROM nursery_schema_migrations"
                ).fetchone()[0]
                indexes = {
                    row[0]
                    for row in connection.execute(
                        "SELECT name FROM sqlite_master WHERE type='index'"
                    )
                }
            self.assertTrue(EXPECTED_TABLES.issubset(tables))
            self.assertTrue(EXPECTED_INDEXES.issubset(indexes))
            self.assertEqual(migration_count, 11)

    def test_failed_migration_rolls_back_partial_schema(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "broken.sqlite3"
            statements = (
                "CREATE TABLE nursery_should_rollback (id INTEGER PRIMARY KEY)",
                "CREATE TABL invalid_sql (id INTEGER)",
            )
            with patch("nursery.store.MIGRATION_1_STATEMENTS", statements):
                with self.assertRaises(sqlite3.OperationalError):
                    NurseryStore(path)
            with closing(sqlite3.connect(path)) as connection:
                tables = {
                    row[0]
                    for row in connection.execute(
                        "SELECT name FROM sqlite_master WHERE type='table'"
                    )
                }
            self.assertNotIn("nursery_should_rollback", tables)
            self.assertNotIn("nursery_schema_migrations", tables)

    def test_existing_stage1_database_migrates_to_stage2(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "stage1.sqlite3"
            with closing(sqlite3.connect(path)) as connection:
                connection.execute(
                    """
                    CREATE TABLE nursery_schema_migrations (
                        schema_version INTEGER PRIMARY KEY,
                        applied_at TEXT NOT NULL,
                        checksum TEXT NOT NULL
                    )
                    """
                )
                for statement in MIGRATION_1_STATEMENTS:
                    connection.execute(statement)
                connection.execute(
                    "INSERT INTO nursery_schema_migrations VALUES (1, ?, ?)",
                    ("2026-08-30T00:00:00+00:00", MIGRATION_1_CHECKSUM),
                )
                connection.commit()
            migrated = NurseryStore(path)
            self.assertEqual(migrated.schema_version(), 11)
            with closing(sqlite3.connect(path)) as connection:
                table = connection.execute(
                    """
                    SELECT name FROM sqlite_master
                    WHERE type='table' AND name='nursery_creation_drafts'
                    """
                ).fetchone()
            self.assertIsNotNone(table)

    def test_v7_care_plan_and_execution_migrate_to_v8_with_composite_fk(self):
        """The v8 table rebuild must retain a real legacy care execution."""

        migrations = (
            (MIGRATION_1_STATEMENTS, MIGRATION_1_CHECKSUM),
            (MIGRATION_2_STATEMENTS, MIGRATION_2_CHECKSUM),
            (MIGRATION_3_STATEMENTS, MIGRATION_3_CHECKSUM),
            (MIGRATION_4_STATEMENTS, MIGRATION_4_CHECKSUM),
            (MIGRATION_5_STATEMENTS, MIGRATION_5_CHECKSUM),
            (MIGRATION_6_STATEMENTS, MIGRATION_6_CHECKSUM),
            (MIGRATION_7_STATEMENTS, MIGRATION_7_CHECKSUM),
            (MIGRATION_8_STATEMENTS, MIGRATION_8_CHECKSUM),
        )
        stamp = "2026-09-03T12:00:00+00:00"
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "stage7.sqlite3"
            with closing(sqlite3.connect(path)) as connection:
                connection.execute("PRAGMA foreign_keys=ON")
                connection.execute(
                    """
                    CREATE TABLE nursery_schema_migrations (
                        schema_version INTEGER PRIMARY KEY,
                        applied_at TEXT NOT NULL,
                        checksum TEXT NOT NULL
                    )
                    """
                )
                for version, (statements, checksum) in enumerate(migrations, start=1):
                    for statement in statements:
                        connection.execute(statement)
                    connection.execute(
                        "INSERT INTO nursery_schema_migrations VALUES (?, ?, ?)",
                        (version, stamp, checksum),
                    )
                connection.execute(
                    """
                    INSERT INTO nursery_modules (
                        account_id, child_id, module_state, state_version,
                        created_at, updated_at
                    ) VALUES ('account', 'child', 'active', 4, ?, ?)
                    """,
                    (stamp, stamp),
                )
                connection.execute(
                    """
                    INSERT INTO nursery_children (
                        child_id, account_id, stage_id, state_version,
                        created_at, updated_at
                    ) VALUES ('child', 'account', 'infancy', 4, ?, ?)
                    """,
                    (stamp, stamp),
                )
                connection.execute(
                    """
                    INSERT INTO nursery_caregivers (
                        caregiver_id, account_id, role, created_at, updated_at
                    ) VALUES ('guardian', 'account', 'user_guardian', ?, ?)
                    """,
                    (stamp, stamp),
                )
                for operation_id in ("legacy-plan-op", "legacy-execution-op", "bad-execution-op"):
                    connection.execute(
                        """
                        INSERT INTO nursery_operations (
                            operation_id, account_id, child_id, caregiver_id,
                            idempotency_key, request_hash, action, status,
                            created_at, updated_at
                        ) VALUES (?, 'account', 'child', 'guardian', ?, 'hash',
                                  'execute_confirmed_care', 'completed', ?, ?)
                        """,
                        (operation_id, operation_id, stamp, stamp),
                    )
                connection.execute(
                    """
                    INSERT INTO nursery_health_events (
                        health_event_id, child_id, status, non_diagnostic_summary,
                        started_at, created_at, updated_at
                    ) VALUES ('legacy-health', 'child', 'caring', '需要照料', ?, ?, ?)
                    """,
                    (stamp, stamp, stamp),
                )
                connection.execute(
                    """
                    INSERT INTO nursery_care_plans (
                        care_plan_id, child_id, health_event_id, plan_version,
                        user_confirmed_summary, status, confirmed_by_user_at,
                        source_operation_id, created_at, updated_at
                    ) VALUES ('legacy-plan', 'child', 'legacy-health', 1,
                              '已确认的现实照料安排', 'confirmed', ?,
                              'legacy-plan-op', ?, ?)
                    """,
                    (stamp, stamp, stamp),
                )
                connection.execute(
                    """
                    INSERT INTO nursery_care_executions (
                        execution_id, care_plan_id, care_plan_version,
                        health_event_id, child_id, caregiver_id,
                        source_operation_id, executed_at
                    ) VALUES ('legacy-execution', 'legacy-plan', 1,
                              'legacy-health', 'child', 'guardian',
                              'legacy-execution-op', ?)
                    """,
                    (stamp,),
                )
                connection.commit()

            migrated = NurseryStore(path)
            self.assertEqual(migrated.schema_version(), 11)
            with closing(sqlite3.connect(path)) as connection:
                connection.execute("PRAGMA foreign_keys=ON")
                plan = connection.execute(
                    "SELECT care_plan_id, plan_version FROM nursery_care_plans"
                ).fetchone()
                execution = connection.execute(
                    """
                    SELECT care_plan_id, care_plan_version, health_event_id
                    FROM nursery_care_executions
                    """
                ).fetchone()
                foreign_keys = connection.execute(
                    "PRAGMA foreign_key_list(nursery_care_executions)"
                ).fetchall()
                self.assertEqual(connection.execute("PRAGMA foreign_key_check").fetchall(), [])
                self.assertEqual(plan, ("legacy-plan", 1))
                self.assertEqual(execution, ("legacy-plan", 1, "legacy-health"))
                plan_fk = [row for row in foreign_keys if row[2] == "nursery_care_plans"]
                self.assertEqual(
                    {(row[1], row[3], row[4]) for row in plan_fk},
                    {(0, "care_plan_id", "care_plan_id"), (1, "care_plan_version", "plan_version")},
                )
                with self.assertRaises(sqlite3.IntegrityError):
                    connection.execute(
                        """
                        INSERT INTO nursery_care_executions (
                            execution_id, care_plan_id, care_plan_version,
                            health_event_id, child_id, caregiver_id,
                            source_operation_id, executed_at
                        ) VALUES ('bad-execution', 'legacy-plan', 2,
                                  'legacy-health', 'child', 'guardian',
                                  'bad-execution-op', ?)
                        """,
                        (stamp,),
                    )

    def test_failed_stage2_migration_leaves_stage1_intact(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "stage1.sqlite3"
            with closing(sqlite3.connect(path)) as connection:
                connection.execute(
                    """
                    CREATE TABLE nursery_schema_migrations (
                        schema_version INTEGER PRIMARY KEY,
                        applied_at TEXT NOT NULL,
                        checksum TEXT NOT NULL
                    )
                    """
                )
                for statement in MIGRATION_1_STATEMENTS:
                    connection.execute(statement)
                connection.execute(
                    "INSERT INTO nursery_schema_migrations VALUES (1, ?, ?)",
                    ("2026-08-30T00:00:00+00:00", MIGRATION_1_CHECKSUM),
                )
                connection.commit()
            statements = (
                "CREATE TABLE nursery_stage2_should_rollback (id INTEGER PRIMARY KEY)",
                "CREATE TABL invalid_stage2_sql (id INTEGER)",
            )
            with patch("nursery.store.MIGRATION_2_STATEMENTS", statements):
                with self.assertRaises(sqlite3.OperationalError):
                    NurseryStore(path)
            with closing(sqlite3.connect(path)) as connection:
                versions = [
                    row[0]
                    for row in connection.execute(
                        "SELECT schema_version FROM nursery_schema_migrations"
                    )
                ]
                partial = connection.execute(
                    """
                    SELECT name FROM sqlite_master
                    WHERE type='table' AND name='nursery_stage2_should_rollback'
                    """
                ).fetchone()
            self.assertEqual(versions, [1])
            self.assertIsNone(partial)

    def test_backup_integrity_and_restore_to_new_file(self):
        with tempfile.TemporaryDirectory() as root:
            root_path = Path(root)
            live = NurseryStore(root_path / "nursery.sqlite3")
            live.create_fixture(account_id="account", child_id="child")
            backup_path = live.backup_to(root_path / "backup.sqlite3")
            restored_path = live.restore_copy_to(root_path / "restored.sqlite3")
            for candidate in (backup_path, restored_path):
                restored = NurseryStore(candidate)
                self.assertEqual(restored.schema_version(), 11)
                self.assertEqual(
                    restored.get_state("account", "child")["module_state"],
                    ModuleState.NEVER_ENABLED.value,
                )

    def test_safety_audit_drops_arbitrary_private_text(self):
        with tempfile.TemporaryDirectory() as root:
            store = NurseryStore(Path(root) / "nursery.sqlite3")
            store.create_fixture(account_id="account", child_id="child")
            store.record_safety_audit(
                SafetyCategory.PRIVACY,
                account_id="account",
                child_id="child",
                error_code="PRIVATE_TEXT_REJECTED",
                metadata={
                    "reason": "blocked",
                    "raw_prompt": "a-private-questionnaire-answer",
                    "api_key": "secret",
                },
            )
            row = store.audit_rows()[0]
            metadata = json.loads(row["metadata_json"])
            self.assertEqual(metadata, {"reason": "blocked"})
            self.assertNotIn("private", row["metadata_json"])
            self.assertNotIn("secret", row["metadata_json"])


class NurseryQueryTests(unittest.TestCase):
    def test_state_specific_whitelists_never_expose_internal_columns(self):
        expected = {
            ModuleState.NEVER_ENABLED: {
                "module",
                "module_state",
                "state_version",
            },
            ModuleState.DRAFT: {
                "module",
                "module_state",
                "state_version",
                "child_id",
                "stage_id",
            },
            ModuleState.ACTIVE: {
                "module",
                "module_state",
                "state_version",
                "child_id",
                "stage_id",
                "active_pause_count",
                "child_state",
                "child_name",
                "sex_status",
            },
            ModuleState.PAUSED: {
                "module",
                "module_state",
                "state_version",
                "child_id",
                "stage_id",
                "active_pause_count",
                "child_state",
                "child_name",
                "sex_status",
            },
            ModuleState.DELETION_PENDING: {
                "module",
                "module_state",
                "state_version",
                "child_id",
                "recycle_deadline",
                "restore_state",
            },
        }
        with tempfile.TemporaryDirectory() as root:
            for index, state in enumerate(ModuleState):
                store = NurseryStore(Path(root) / f"nursery-{index}.sqlite3")
                store.create_fixture(
                    account_id=f"account-{index}",
                    child_id=f"child-{index}",
                    state=state,
                )
                status = NurseryQueryService(store).get_status(
                    account_id=f"account-{index}", child_id=f"child-{index}"
                )
                self.assertEqual(set(status), expected[state])
                self.assertNotIn("pre_delete_state", status)
                self.assertNotIn("payload_json", status)
                self.assertNotIn("caregiver_id", status)

    def test_repeated_status_reads_create_no_operation_health_or_version_change(self):
        with tempfile.TemporaryDirectory() as root:
            store = NurseryStore(Path(root) / "nursery.sqlite3")
            store.create_fixture(
                account_id="account", child_id="child", state=ModuleState.ACTIVE
            )
            query = NurseryQueryService(store)
            before = store.get_state("account", "child")
            for _ in range(50):
                self.assertEqual(
                    query.get_status(account_id="account", child_id="child")[
                        "module_state"
                    ],
                    "active",
                )
            after = store.get_state("account", "child")
            self.assertEqual(before["state_version"], after["state_version"])
            self.assertEqual(store.count_rows("nursery_operations"), 0)
            self.assertEqual(store.count_rows("nursery_health_evaluations"), 0)


class NurseryBodyTimeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.clock = MutableClock()
        self.store = NurseryStore(Path(self.temp.name) / "nursery.sqlite3", clock=self.clock)
        self.store.create_fixture(
            account_id="account",
            child_id="child",
            state=ModuleState.ACTIVE,
        )
        self.actor = ActorContext.build(
            account_id="account",
            caregiver_id="user-guardian",
            role=CaregiverRole.USER_GUARDIAN,
            permissions=("nursery.*",),
            auth_source=AuthSource.TEST_FIXTURE,
        )
        self.store.register_caregiver(self.actor)
        self.rule = BodyTimeRule(
            version="body-time-test-v1",
            per_hour={"hunger": 0.2, "thirst": 0.1, "fatigue": 0.3},
            maximum={"hunger": 0.6, "thirst": 0.7, "fatigue": 0.8},
            notice_at={"hunger": 0.5, "thirst": 0.5, "fatigue": 0.5},
        )
        with self.store._write() as connection:  # isolated fixture only
            stamp = self.clock().isoformat(timespec="microseconds")
            connection.execute(
                """
                INSERT INTO nursery_body_clocks (
                    child_id, settled_through, rule_version, updated_at
                ) VALUES ('child', ?, 'body-time-test-v1', ?)
                """,
                (stamp, stamp),
            )

    async def asyncTearDown(self):
        self.temp.cleanup()

    def command(self, action: NurseryAction) -> NurseryCommand:
        return NurseryCommand(
            actor=self.actor,
            child_id="child",
            action=action,
            operation_id=f"00000000-0000-4000-8000-{self.store.count_rows('nursery_operations') + 1:012d}",
            idempotency_key=f"body-time-{self.store.count_rows('nursery_operations') + 1}",
            expected_state_version=None,
            payload={},
            source=SourceReference.build(
                source_type=SourceType.TEST_FIXTURE,
                source_id=f"body-time-source-{self.store.count_rows('nursery_operations') + 1}",
                source_version="v1",
                specification_ref="body-time-test",
            ),
        )

    def test_preview_and_effective_view_are_read_only(self):
        self.clock.advance(hours=1)
        before_state = self.store.get_child_runtime_state("child")
        before_clock = self.store.get_body_clock("child")
        preview = self.store.preview_body_settlement(
            child_id="child", through=self.clock(), rule=self.rule
        )
        view = effective_body_view(
            before_state,
            before_clock,
            evaluated_at=self.clock(),
            rule=self.rule,
            active=True,
        )
        self.assertEqual(preview["runtime_state"]["hunger"], 0.4)
        self.assertTrue(view["time_context"]["settlement_due"])
        self.assertIn("fatigue", view["body_needs"])
        self.assertEqual(self.store.get_child_runtime_state("child"), before_state)
        self.assertEqual(self.store.get_body_clock("child"), before_clock)

    def test_disabled_rule_is_noop_and_configured_rule_is_deterministic_capped(self):
        self.clock.advance(hours=10)
        initial = self.store.get_child_runtime_state("child")
        self.store.settle_body_time(
            child_id="child", through=self.clock(), rule=BodyTimeRule()
        )
        self.assertEqual(self.store.get_child_runtime_state("child"), initial)
        self.clock.advance(hours=10)
        self.store.settle_body_time(
            child_id="child", through=self.clock(), rule=self.rule
        )
        settled = self.store.get_child_runtime_state("child")
        self.assertEqual(
            {name: settled[name] for name in ("hunger", "thirst", "fatigue")},
            {"hunger": 0.6, "thirst": 0.7, "fatigue": 0.8},
        )

    def test_same_interval_consumes_once_and_never_changes_unwell(self):
        with self.store._write() as connection:  # isolated fixture only
            runtime = self.store.get_child_runtime_state("child")
            runtime["unwell"] = 0.4
            connection.execute(
                "INSERT INTO nursery_child_runtime_state VALUES (?, ?, ?)",
                ("child", json.dumps(runtime), self.clock().isoformat()),
            )
        self.clock.advance(hours=1)
        first = self.store.settle_body_time(
            child_id="child", through=self.clock(), rule=self.rule
        )
        state_after_first = self.store.get_child_runtime_state("child")
        second = self.store.settle_body_time(
            child_id="child", through=self.clock(), rule=self.rule
        )
        self.assertEqual(first, second)
        self.assertEqual(self.store.get_child_runtime_state("child"), state_after_first)
        self.assertEqual(state_after_first["unwell"], 0.4)

    async def test_pause_and_deletion_intervals_are_frozen(self):
        coordinator = NurseryCoordinator(self.store)
        self.clock.advance(hours=1)
        self.store.settle_body_time(child_id="child", through=self.clock(), rule=self.rule)
        before_pause = self.store.get_child_runtime_state("child")
        paused = await coordinator.submit(self.command(NurseryAction.PAUSE))
        self.assertEqual(paused.status, OperationStatus.COMPLETED)
        self.clock.advance(hours=5)
        self.store.settle_body_time(child_id="child", through=self.clock(), rule=self.rule)
        self.assertEqual(self.store.get_child_runtime_state("child"), before_pause)
        resumed = await coordinator.submit(self.command(NurseryAction.RESUME))
        self.assertEqual(resumed.status, OperationStatus.COMPLETED)
        self.clock.advance(hours=1)
        self.store.settle_body_time(child_id="child", through=self.clock(), rule=self.rule)
        after_resume = self.store.get_child_runtime_state("child")
        self.assertEqual(after_resume["hunger"], 0.6)
        self.assertEqual(after_resume["thirst"], 0.4)

        with self.store._write() as connection:  # make a deletion-pending fixture
            connection.execute(
                """
                UPDATE nursery_modules
                SET module_state='deletion_pending', pre_delete_state='active',
                    recycle_deadline=?, state_version=state_version + 1
                WHERE child_id='child'
                """,
                ((self.clock() + timedelta(days=30)).isoformat(),),
            )
            connection.execute(
                "UPDATE nursery_children SET state_version=state_version + 1 WHERE child_id='child'"
            )
        self.clock.advance(hours=5)
        self.store.settle_body_time(child_id="child", through=self.clock(), rule=self.rule)
        self.assertEqual(self.store.get_child_runtime_state("child"), after_resume)
        cancelled = await coordinator.submit(self.command(NurseryAction.CANCEL_DELETION))
        self.assertEqual(cancelled.status, OperationStatus.COMPLETED)
        self.clock.advance(hours=1)
        self.store.settle_body_time(child_id="child", through=self.clock(), rule=self.rule)
        after_restore = self.store.get_child_runtime_state("child")
        self.assertEqual(after_restore["thirst"], 0.5)

    async def test_activation_initializes_clock_at_activation_time(self):
        with tempfile.TemporaryDirectory() as root:
            store = NurseryStore(Path(root) / "activation.sqlite3", clock=self.clock)
            store.create_fixture(
                account_id="activation-account",
                child_id="activation-child",
                state=ModuleState.DRAFT,
            )
            actor = ActorContext.build(
                account_id="activation-account",
                caregiver_id="activation-guardian",
                role=CaregiverRole.USER_GUARDIAN,
                permissions=("nursery.*",),
                auth_source=AuthSource.TEST_FIXTURE,
            )
            store.register_caregiver(actor)
            self.clock.advance(days=2)
            command = NurseryCommand(
                actor=actor,
                child_id="activation-child",
                action=NurseryAction.ACTIVATE_FIXTURE,
                operation_id="00000000-0000-4000-8000-000000000099",
                idempotency_key="activate-clock",
                expected_state_version=0,
                payload={},
                source=SourceReference.build(
                    source_type=SourceType.TEST_FIXTURE,
                    source_id="activate-clock",
                    source_version="v1",
                ),
            )
            result = await NurseryCoordinator(store).submit(command)
            self.assertEqual(result.status, OperationStatus.COMPLETED)
            self.assertEqual(
                store.get_body_clock("activation-child")["settled_through"],
                self.clock().isoformat(timespec="microseconds"),
            )


class NurseryHealthContractTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.clock = MutableClock()
        self.store = NurseryStore(
            Path(self.temp.name) / "nursery.sqlite3", clock=self.clock
        )
        self.store.create_fixture(
            account_id="account", child_id="child", state=ModuleState.ACTIVE
        )

    def tearDown(self):
        self.temp.cleanup()

    @staticmethod
    def conditions():
        return {
            "age_stage": "infancy",
            "activity": "ordinary_interaction",
            "room_environment": {"confirmed_safe": True},
            "confirmed_interaction": True,
            "long_term_stress": {"independent_unresolved_conflicts": 0},
            "protective_factors": ["stable_care"],
            "existing_health_event": None,
        }

    def test_same_evaluation_key_runs_once_and_replays_same_result(self):
        service = HealthEvaluationService(
            self.store, FixedHealthRule(HealthOutcome.NO_CHANGE)
        )
        first = service.evaluate(
            account_id="account",
            child_id="child",
            trigger=HealthTrigger.INTERACTION,
            source=source(1),
            conditions=self.conditions(),
        )
        second = service.evaluate(
            account_id="account",
            child_id="child",
            trigger=HealthTrigger.INTERACTION,
            source=source(1),
            conditions={**self.conditions(), "activity": "changed-but-same-source"},
        )
        self.assertEqual(first["evaluation_key"], second["evaluation_key"])
        self.assertFalse(first["replayed"])
        self.assertTrue(second["replayed"])
        self.assertEqual(self.store.count_rows("nursery_health_evaluations"), 1)

    def test_twenty_concurrent_health_retries_execute_fixed_rule_once(self):
        class CountingRule:
            rule_version = "counting-v1"

            def __init__(self):
                self.calls = 0
                self.lock = __import__("threading").Lock()

            def evaluate(self, conditions):
                with self.lock:
                    self.calls += 1
                return HealthOutcome.NO_CHANGE

        rule = CountingRule()
        service = HealthEvaluationService(self.store, rule)

        def evaluate_once(_):
            return service.evaluate(
                account_id="account",
                child_id="child",
                trigger=HealthTrigger.INTERACTION,
                source=source(12),
                conditions=self.conditions(),
            )

        with ThreadPoolExecutor(max_workers=20) as executor:
            results = list(executor.map(evaluate_once, range(20)))
        self.assertEqual(rule.calls, 1)
        self.assertEqual({item["evaluation_key"] for item in results}, {
            results[0]["evaluation_key"]
        })
        self.assertEqual(self.store.count_rows("nursery_health_evaluations"), 1)

    def test_refresh_status_read_and_page_open_cannot_evaluate(self):
        service = HealthEvaluationService(self.store, FixedHealthRule())
        for trigger in (
            HealthTrigger.REFRESH,
            HealthTrigger.STATUS_READ,
            HealthTrigger.PAGE_OPEN,
            HealthTrigger.POLL,
        ):
            with self.subTest(trigger=trigger.value):
                with self.assertRaises(NurseryError) as caught:
                    service.evaluate(
                        account_id="account",
                        child_id="child",
                        trigger=trigger,
                        source=source(2),
                        conditions=self.conditions(),
                    )
                self.assertEqual(caught.exception.code, "HEALTH_TRIGGER_NOT_ALLOWED")
        self.assertEqual(self.store.count_rows("nursery_health_evaluations"), 0)

    def test_forbidden_severe_result_is_audited_without_changing_child(self):
        service = HealthEvaluationService(
            self.store, FixedHealthRule("severe_illness")
        )
        before = self.store.get_state("account", "child")
        with self.assertRaises(NurseryError) as caught:
            service.evaluate(
                account_id="account",
                child_id="child",
                trigger=HealthTrigger.VALID_WAKE,
                source=source(3),
                conditions=self.conditions(),
            )
        after = self.store.get_state("account", "child")
        self.assertEqual(caught.exception.code, "HEALTH_OUTCOME_FORBIDDEN")
        self.assertEqual(before["state_version"], after["state_version"])
        self.assertEqual(self.store.count_rows("nursery_health_evaluations"), 0)
        self.assertEqual(self.store.count_rows("nursery_safety_audit"), 1)

    def test_non_active_state_freezes_health(self):
        with tempfile.TemporaryDirectory() as root:
            store = NurseryStore(Path(root) / "paused.sqlite3")
            store.create_fixture(
                account_id="account", child_id="child", state=ModuleState.PAUSED
            )
            service = HealthEvaluationService(store, FixedHealthRule())
            with self.assertRaises(NurseryError) as caught:
                service.evaluate(
                    account_id="account",
                    child_id="child",
                    trigger=HealthTrigger.INTERACTION,
                    source=source(4),
                    conditions=self.conditions(),
                )
            self.assertEqual(caught.exception.code, "HEALTH_FROZEN")
            self.assertEqual(store.count_rows("nursery_health_evaluations"), 0)

    def test_explicit_frequency_cooldown_and_active_event_guards(self):
        frequency = HealthEvaluationService(
            self.store,
            FixedHealthRule(HealthOutcome.NO_CHANGE),
            guard=HealthGuardConfig(
                max_evaluations_per_window=1,
                window_seconds=60,
                block_when_active_event=False,
            ),
        )
        frequency.evaluate(
            account_id="account",
            child_id="child",
            trigger=HealthTrigger.INTERACTION,
            source=source(5),
            conditions=self.conditions(),
        )
        with self.assertRaises(NurseryError) as limited:
            frequency.evaluate(
                account_id="account",
                child_id="child",
                trigger=HealthTrigger.INTERACTION,
                source=source(6),
                conditions=self.conditions(),
            )
        self.assertEqual(limited.exception.code, "HEALTH_FREQUENCY_GUARD")

        self.clock.advance(seconds=61)
        active = HealthEvaluationService(
            self.store,
            FixedHealthRule(HealthOutcome.OBSERVE),
            guard=HealthGuardConfig(block_when_active_event=False),
        )
        active.evaluate(
            account_id="account",
            child_id="child",
            trigger=HealthTrigger.RELEVANT_MAILBOX_EVENT,
            source=source(7),
            conditions=self.conditions(),
        )
        blocked = HealthEvaluationService(
            self.store,
            FixedHealthRule(HealthOutcome.NO_CHANGE),
            guard=HealthGuardConfig(block_when_active_event=True),
        )
        with self.assertRaises(NurseryError) as existing:
            blocked.evaluate(
                account_id="account",
                child_id="child",
                trigger=HealthTrigger.INTERACTION,
                source=source(8),
                conditions=self.conditions(),
            )
        self.assertEqual(existing.exception.code, "HEALTH_EVENT_ALREADY_ACTIVE")

    def test_rule_exception_timeout_and_missing_conditions_write_nothing(self):
        class BrokenRule:
            rule_version = "broken-v1"

            def __init__(self, error):
                self.error = error

            def evaluate(self, conditions):
                raise self.error

        for error in (RuntimeError("broken"), TimeoutError("timeout")):
            service = HealthEvaluationService(self.store, BrokenRule(error))
            with self.assertRaises(type(error)):
                service.evaluate(
                    account_id="account",
                    child_id="child",
                    trigger=HealthTrigger.INTERACTION,
                    source=source(9 if isinstance(error, RuntimeError) else 10),
                    conditions=self.conditions(),
                )
        with self.assertRaises(NurseryError) as missing:
            HealthEvaluationService(self.store, FixedHealthRule()).evaluate(
                account_id="account",
                child_id="child",
                trigger=HealthTrigger.INTERACTION,
                source=source(11),
                conditions={},
            )
        self.assertEqual(missing.exception.code, "HEALTH_SOURCE_CONDITIONS_REQUIRED")
        self.assertEqual(self.store.count_rows("nursery_health_evaluations"), 0)


if __name__ == "__main__":
    unittest.main()
