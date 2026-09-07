from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

from nursery.coordinator import NurseryCoordinator
from nursery.external_tools import EXTERNAL_TOOL_PERMISSION, ExternalNurseryTools
from nursery.feature_service import NurseryFeatureService
from nursery.models import (
    ActorContext,
    AuthSource,
    CaregiverRole,
    ModuleState,
    NurseryAction,
    NurseryCommand,
    NurseryError,
    OperationStatus,
    SourceReference,
    SourceType,
)
from nursery.queries import NurseryQueryService
from nursery.store import NurseryStore


class MutableClock:
    def __init__(self) -> None:
        self.value = datetime(2026, 9, 3, 12, 0, tzinfo=timezone.utc)

    def __call__(self) -> datetime:
        return self.value

    def advance(self, **kwargs: int) -> None:
        self.value += timedelta(**kwargs)


class NurseryCareClosureTests(unittest.IsolatedAsyncioTestCase):
    """The health event -> observation -> confirmed-care execution boundary."""

    async def asyncSetUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.clock = MutableClock()
        self.store = NurseryStore(
            Path(self.temp.name) / "nursery.sqlite3", clock=self.clock
        )
        self.store.create_fixture(
            account_id="account", child_id="child", state=ModuleState.ACTIVE
        )
        self.user = ActorContext.build(
            account_id="account",
            caregiver_id="user-guardian",
            role=CaregiverRole.USER_GUARDIAN,
            permissions=("nursery.*",),
            auth_source=AuthSource.MANAGER_SESSION,
        )
        self.external = ActorContext.build(
            account_id="account",
            caregiver_id="external-guardian",
            role=CaregiverRole.EXTERNAL_AI_GUARDIAN,
            permissions=(EXTERNAL_TOOL_PERMISSION,),
            auth_source=AuthSource.MCP_SESSION,
        )
        self.store.register_caregiver(self.user)
        self.store.register_caregiver(self.external)
        self.feature = NurseryFeatureService(self.store)
        self.serial = 0

    async def asyncTearDown(self) -> None:
        self.temp.cleanup()

    def source(self) -> SourceReference:
        self.serial += 1
        return SourceReference.build(
            source_type=SourceType.TEST_FIXTURE,
            source_id=f"care-closure-source-{self.serial}",
            source_version="v1",
            specification_ref="care-closure-test-v1",
        )

    def command(
        self,
        *,
        actor: ActorContext,
        action: NurseryAction,
        payload: dict,
        expected_state_version: int | None,
    ) -> NurseryCommand:
        operation_id = str(uuid.uuid4())
        return NurseryCommand(
            actor=actor,
            child_id="child",
            action=action,
            operation_id=operation_id,
            idempotency_key=operation_id,
            expected_state_version=expected_state_version,
            payload=payload,
            source=self.source(),
        )

    def add_health_event(self, *, status: str = "monitoring") -> str:
        health_event_id = str(uuid.uuid4())
        stamp = self.clock().isoformat(timespec="microseconds")
        with self.store._write() as connection:  # isolated fixture only
            connection.execute(
                """
                INSERT INTO nursery_health_events (
                    health_event_id, child_id, status, non_diagnostic_summary,
                    observed_effects_json, condition_summary_json, started_at,
                    recovered_at, created_at, updated_at
                ) VALUES (?, 'child', ?, '精神不太好，需要继续留意', ?, ?, ?, NULL, ?, ?)
                """,
                (
                    health_event_id,
                    status,
                    json.dumps({"energy": "lower"}),
                    json.dumps(["ordinary interaction"]),
                    stamp,
                    stamp,
                    stamp,
                ),
            )
        return health_event_id

    async def save_confirmed_plan(
        self, *, health_event_id: str, care_plan_id: str = "care-plan-1", summary: str = "按现实中已确认的照料安排陪伴和休息"
    ) -> dict:
        operation_id = str(uuid.uuid4())
        return await self.feature.submit(
            actor=self.user,
            child_id="child",
            action=NurseryAction.SAVE_CONFIRMED_CARE_PLAN,
            payload={
                "care_plan_id": care_plan_id,
                "health_event_id": health_event_id,
                "user_confirmed_summary": summary,
            },
            source=self.source(),
            operation_id=operation_id,
            idempotency_key=operation_id,
            expected_state_version=None,
        )

    async def test_user_can_confirm_versioned_plan_but_mcp_cannot_create_one(self) -> None:
        health_event_id = self.add_health_event()
        first = await self.save_confirmed_plan(health_event_id=health_event_id)
        self.assertEqual(first["status"], OperationStatus.COMPLETED.value, first)
        self.assertEqual(first["result"]["care_plan_id"], "care-plan-1")
        self.assertEqual(first["result"]["care_plan_version"], 1)
        self.assertEqual(first["result"]["care_plan_status"], "confirmed")

        self.clock.advance(minutes=1)
        second = await self.save_confirmed_plan(
            health_event_id=health_event_id,
            summary="更新后的现实照料安排",
        )
        self.assertEqual(second["status"], OperationStatus.COMPLETED.value, second)
        self.assertEqual(second["result"]["care_plan_version"], 2)
        with self.store._read() as connection:
            versions = connection.execute(
                """
                SELECT plan_version, status, user_confirmed_summary
                FROM nursery_care_plans WHERE care_plan_id='care-plan-1'
                ORDER BY plan_version
                """
            ).fetchall()
        self.assertEqual(
            [(row["plan_version"], row["status"], row["user_confirmed_summary"]) for row in versions],
            [
                (1, "withdrawn", "按现实中已确认的照料安排陪伴和休息"),
                (2, "confirmed", "更新后的现实照料安排"),
            ],
        )

        tools = ExternalNurseryTools(store=self.store, creation=None)  # type: ignore[arg-type]
        with self.assertRaises(NurseryError) as denied:
            await tools.feature_update(
                actor=self.external,
                tool_group="care",
                action="save_confirmed_care_plan",
                child_id="child",
                payload={
                    "care_plan_id": "outside-plan",
                    "health_event_id": health_event_id,
                    "user_confirmed_summary": "外部 AI 不可写入",
                },
            )
        self.assertEqual(denied.exception.code, "EXTERNAL_TOOL_ACTION_NOT_AVAILABLE")

    async def test_care_plan_id_cannot_be_reused_for_a_different_health_event(self) -> None:
        first_event = self.add_health_event()
        first = await self.save_confirmed_plan(
            health_event_id=first_event, care_plan_id="stable-plan-id"
        )
        self.assertEqual(first["status"], OperationStatus.COMPLETED.value, first)
        second_event = self.add_health_event()

        conflicting = await self.save_confirmed_plan(
            health_event_id=second_event, care_plan_id="stable-plan-id"
        )
        self.assertEqual(conflicting["status"], OperationStatus.REJECTED.value, conflicting)
        self.assertEqual(conflicting["error"]["code"], "CARE_PLAN_ID_CONFLICT")
        with self.store._read() as connection:
            rows = connection.execute(
                """
                SELECT health_event_id, plan_version FROM nursery_care_plans
                WHERE care_plan_id='stable-plan-id' ORDER BY plan_version
                """
            ).fetchall()
        self.assertEqual([(row["health_event_id"], row["plan_version"]) for row in rows], [(first_event, 1)])

    async def test_resolved_event_rejects_first_execution_but_replays_existing_execution(self) -> None:
        for status in ("recovered", "corrected"):
            with self.subTest(status=status):
                health_event_id = self.add_health_event()
                plan = await self.save_confirmed_plan(
                    health_event_id=health_event_id,
                    care_plan_id=f"{status}-first-execution",
                )
                with self.store._write() as connection:
                    connection.execute(
                        "UPDATE nursery_health_events SET status=?, recovered_at=? WHERE health_event_id=?",
                        (status, self.clock().isoformat(timespec="microseconds"), health_event_id),
                    )
                rejected = await NurseryCoordinator(self.store).submit(
                    self.command(
                        actor=self.external,
                        action=NurseryAction.EXECUTE_CONFIRMED_CARE,
                        payload={
                            "care_plan_id": f"{status}-first-execution",
                            "care_plan_version": plan["result"]["care_plan_version"],
                            "health_event_id": health_event_id,
                        },
                        expected_state_version=plan["state_version"],
                    )
                )
                self.assertEqual(rejected.status, OperationStatus.REJECTED, rejected)
                self.assertEqual(rejected.error_code, "HEALTH_EVENT_NOT_ACTIVE")
                with self.store._read() as connection:
                    first_execution_count = connection.execute(
                        "SELECT COUNT(*) FROM nursery_care_executions WHERE health_event_id=?",
                        (health_event_id,),
                    ).fetchone()[0]
                self.assertEqual(first_execution_count, 0)

                # Use a separate fresh event for the valid earlier record, then resolve it.
                replay_event_id = self.add_health_event()
                replay_plan = await self.save_confirmed_plan(
                    health_event_id=replay_event_id,
                    care_plan_id=f"{status}-existing-execution",
                )
                first = await NurseryCoordinator(self.store).submit(
                    self.command(
                        actor=self.user,
                        action=NurseryAction.EXECUTE_CONFIRMED_CARE,
                        payload={
                            "care_plan_id": f"{status}-existing-execution",
                            "care_plan_version": replay_plan["result"]["care_plan_version"],
                            "health_event_id": replay_event_id,
                        },
                        expected_state_version=replay_plan["state_version"],
                    )
                )
                self.assertEqual(first.status, OperationStatus.COMPLETED, first)
                with self.store._write() as connection:
                    connection.execute(
                        "UPDATE nursery_health_events SET status=?, recovered_at=? WHERE health_event_id=?",
                        (status, self.clock().isoformat(timespec="microseconds"), replay_event_id),
                    )
                replay = await NurseryCoordinator(self.store).submit(
                    self.command(
                        actor=self.external,
                        action=NurseryAction.EXECUTE_CONFIRMED_CARE,
                        payload={
                            "care_plan_id": f"{status}-existing-execution",
                            "care_plan_version": replay_plan["result"]["care_plan_version"],
                            "health_event_id": replay_event_id,
                        },
                        expected_state_version=replay_plan["state_version"],
                    )
                )
                self.assertEqual(replay.status, OperationStatus.COMPLETED, replay)
                self.assertFalse(replay.changed)
                self.assertTrue(replay.result["already_recorded"])
                self.assertEqual(replay.result["execution_id"], first.result["execution_id"])

    async def test_superseded_plan_version_replays_its_existing_execution(self) -> None:
        health_event_id = self.add_health_event()
        first_plan = await self.save_confirmed_plan(
            health_event_id=health_event_id, care_plan_id="versioned-execution"
        )
        first_execution = await NurseryCoordinator(self.store).submit(
            self.command(
                actor=self.user,
                action=NurseryAction.EXECUTE_CONFIRMED_CARE,
                payload={
                    "care_plan_id": "versioned-execution",
                    "care_plan_version": first_plan["result"]["care_plan_version"],
                    "health_event_id": health_event_id,
                },
                expected_state_version=first_plan["state_version"],
            )
        )
        self.assertEqual(first_execution.status, OperationStatus.COMPLETED, first_execution)
        replacement = await self.save_confirmed_plan(
            health_event_id=health_event_id,
            care_plan_id="versioned-execution",
            summary="更新后的现实照料安排",
        )
        self.assertEqual(replacement["result"]["care_plan_version"], 2)

        replay = await NurseryCoordinator(self.store).submit(
            self.command(
                actor=self.external,
                action=NurseryAction.EXECUTE_CONFIRMED_CARE,
                payload={
                    "care_plan_id": "versioned-execution",
                    "care_plan_version": 1,
                    "health_event_id": health_event_id,
                },
                expected_state_version=replacement["state_version"],
            )
        )
        self.assertEqual(replay.status, OperationStatus.COMPLETED, replay)
        self.assertFalse(replay.changed)
        self.assertTrue(replay.result["already_recorded"])
        self.assertEqual(replay.result["execution_id"], first_execution.result["execution_id"])

    async def test_phone_and_mcp_concurrent_execution_records_once_with_real_executor(self) -> None:
        health_event_id = self.add_health_event()
        plan = await self.save_confirmed_plan(health_event_id=health_event_id)
        plan_version = plan["result"]["care_plan_version"]
        expected_version = plan["state_version"]
        payload = {
            "care_plan_id": "care-plan-1",
            "care_plan_version": plan_version,
            "health_event_id": health_event_id,
        }
        phone = NurseryCoordinator(self.store)
        mcp = NurseryCoordinator(self.store)
        first, second = await asyncio.gather(
            phone.submit(
                self.command(
                    actor=self.user,
                    action=NurseryAction.EXECUTE_CONFIRMED_CARE,
                    payload=payload,
                    expected_state_version=expected_version,
                )
            ),
            mcp.submit(
                self.command(
                    actor=self.external,
                    action=NurseryAction.EXECUTE_CONFIRMED_CARE,
                    payload=payload,
                    expected_state_version=expected_version,
                )
            ),
        )
        self.assertEqual(first.status, OperationStatus.COMPLETED, first)
        self.assertEqual(second.status, OperationStatus.COMPLETED, second)
        actual = next(result for result in (first, second) if result.changed)
        duplicate = next(result for result in (first, second) if not result.changed)
        self.assertTrue(duplicate.result["already_recorded"])
        self.assertEqual(duplicate.result["execution_id"], actual.result["execution_id"])
        self.assertEqual(duplicate.result["executed_at"], actual.result["executed_at"])
        self.assertEqual(duplicate.result["executed_by"], actual.result["executed_by"])
        self.assertIn(actual.result["executed_by"], {"user-guardian", "external-guardian"})

        with self.store._read() as connection:
            execution_count = connection.execute(
                "SELECT COUNT(*) FROM nursery_care_executions"
            ).fetchone()[0]
            event_status = connection.execute(
                "SELECT status FROM nursery_health_events WHERE health_event_id=?",
                (health_event_id,),
            ).fetchone()[0]
        self.assertEqual(execution_count, 1)
        self.assertEqual(event_status, "caring")
        self.assertEqual(
            self.store.get_body_clock("child")["last_care_at"],
            actual.result["executed_at"],
        )
        self.assertEqual(
            self.store.get_state("account", "child")["state_version"],
            expected_version + 1,
        )

    async def test_phone_and_mcp_share_a_read_only_care_view(self) -> None:
        health_event_id = self.add_health_event()
        plan = await self.save_confirmed_plan(health_event_id=health_event_id)
        execution = await NurseryCoordinator(self.store).submit(
            self.command(
                actor=self.user,
                action=NurseryAction.EXECUTE_CONFIRMED_CARE,
                payload={
                    "care_plan_id": "care-plan-1",
                    "care_plan_version": plan["result"]["care_plan_version"],
                    "health_event_id": health_event_id,
                },
                expected_state_version=plan["state_version"],
            )
        )
        self.assertEqual(execution.status, OperationStatus.COMPLETED, execution)
        before_version = self.store.get_state("account", "child")["state_version"]
        before_operations = self.store.count_rows("nursery_operations")
        before_evaluations = self.store.count_rows("nursery_health_evaluations")
        before_executions = self.store.count_rows("nursery_care_executions")

        phone_view = NurseryQueryService(self.store).get_child_status(
            actor=self.user, child_id="child", detail="care"
        )
        mcp_view = ExternalNurseryTools(
            store=self.store, creation=None  # type: ignore[arg-type]
        ).child_status(actor=self.external, child_id="child", detail="care")
        self.assertEqual(phone_view["care_view"], mcp_view["care_view"])
        care_view = phone_view["care_view"]
        self.assertEqual(care_view["active_events"][0]["health_event_id"], health_event_id)
        self.assertEqual(care_view["confirmed_plans"][0]["plan_version"], 1)
        self.assertEqual(care_view["executions"][0]["execution_id"], execution.result["execution_id"])
        self.assertEqual(care_view["executions"][0]["executed_by"], "user-guardian")

        for _ in range(10):
            NurseryQueryService(self.store).get_child_status(
                actor=self.user, child_id="child", detail="care"
            )
            ExternalNurseryTools(
                store=self.store, creation=None  # type: ignore[arg-type]
            ).child_status(actor=self.external, child_id="child", detail="care")
        self.assertEqual(
            self.store.get_state("account", "child")["state_version"], before_version
        )
        self.assertEqual(self.store.count_rows("nursery_operations"), before_operations)
        self.assertEqual(
            self.store.count_rows("nursery_health_evaluations"), before_evaluations
        )
        self.assertEqual(self.store.count_rows("nursery_care_executions"), before_executions)


if __name__ == "__main__":
    unittest.main()
