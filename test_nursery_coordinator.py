from __future__ import annotations

import asyncio
import json
import sqlite3
import tempfile
import unittest
import uuid
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path

from nursery.coordinator import NurseryCoordinator
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
from nursery.operations import request_hash
from nursery.queue import ChildLockRegistry, QueueConfig
from nursery.store import NurseryStore


class MutableClock:
    def __init__(self):
        self.value = datetime(2026, 8, 30, 12, 0, tzinfo=timezone.utc)

    def __call__(self):
        return self.value

    def advance(self, **kwargs):
        self.value += timedelta(**kwargs)


def guardian(account_id: str, caregiver_id: str, role: CaregiverRole) -> ActorContext:
    return ActorContext.build(
        account_id=account_id,
        caregiver_id=caregiver_id,
        role=role,
        permissions=["nursery.*"],
        auth_source=AuthSource.TEST_FIXTURE,
    )


class NurseryCoordinatorTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.clock = MutableClock()
        self.db_path = Path(self.temp.name) / "nursery.sqlite3"
        self.store = NurseryStore(self.db_path, clock=self.clock)
        self.user = guardian(
            "account", "user-guardian", CaregiverRole.USER_GUARDIAN
        )
        self.external = guardian(
            "account", "external-ai", CaregiverRole.EXTERNAL_AI_GUARDIAN
        )
        self.serial = 0

    async def asyncTearDown(self):
        self.temp.cleanup()

    def setup_child(self, state: ModuleState = ModuleState.NEVER_ENABLED):
        self.store.create_fixture(
            account_id="account", child_id="child", state=state
        )
        self.store.register_caregiver(self.user)
        self.store.register_caregiver(self.external)

    def command(
        self,
        action: NurseryAction,
        *,
        actor: ActorContext | None = None,
        child_id: str = "child",
        expected: int | None = None,
        payload: dict | None = None,
        operation_id: str | None = None,
        idempotency_key: str | None = None,
        source_id: str | None = None,
        source_type: SourceType = SourceType.TEST_FIXTURE,
    ) -> NurseryCommand:
        self.serial += 1
        return NurseryCommand(
            actor=actor or self.user,
            child_id=child_id,
            action=action,
            operation_id=operation_id or str(uuid.uuid4()),
            idempotency_key=idempotency_key or f"idempotency-{self.serial}",
            expected_state_version=expected,
            payload=dict(payload or {}),
            source=SourceReference.build(
                source_type=source_type,
                source_id=source_id or f"source-{self.serial}",
                source_version="v1",
                specification_ref="stage1-coordinator-test",
            ),
        )

    async def test_full_five_state_flow_pause_consensus_restore_and_cleanup(self):
        self.setup_child()
        coordinator = NurseryCoordinator(self.store)

        draft = await coordinator.submit(
            self.command(NurseryAction.START_DRAFT, expected=0)
        )
        active = await coordinator.submit(
            self.command(NurseryAction.ACTIVATE_FIXTURE, expected=1)
        )
        paused = await coordinator.submit(
            self.command(NurseryAction.PAUSE, expected=2)
        )
        second_pause = await coordinator.submit(
            self.command(NurseryAction.PAUSE, actor=self.external, expected=3)
        )
        first_resume = await coordinator.submit(
            self.command(NurseryAction.RESUME, expected=3)
        )
        final_resume = await coordinator.submit(
            self.command(NurseryAction.RESUME, actor=self.external, expected=3)
        )

        self.assertEqual(draft.module_state, ModuleState.DRAFT)
        self.assertEqual(active.module_state, ModuleState.ACTIVE)
        self.assertEqual(paused.module_state, ModuleState.PAUSED)
        self.assertFalse(second_pause.changed)
        self.assertEqual(first_resume.module_state, ModuleState.PAUSED)
        self.assertFalse(first_resume.changed)
        self.assertEqual(final_resume.module_state, ModuleState.ACTIVE)
        self.assertEqual(final_resume.state_version, 4)

        first_delete = await coordinator.submit(
            self.command(
                NurseryAction.CONFIRM_DELETION,
                expected=4,
                payload={"subject_version": 4},
            )
        )
        second_delete = await coordinator.submit(
            self.command(
                NurseryAction.CONFIRM_DELETION,
                actor=self.external,
                expected=4,
                payload={"subject_version": 4},
            )
        )
        self.assertFalse(first_delete.result["confirmation_complete"])
        self.assertEqual(first_delete.module_state, ModuleState.ACTIVE)
        self.assertTrue(second_delete.result["confirmation_complete"])
        self.assertEqual(second_delete.module_state, ModuleState.DELETION_PENDING)
        self.assertEqual(second_delete.state_version, 5)

        cancelled = await coordinator.submit(
            self.command(NurseryAction.CANCEL_DELETION, expected=5)
        )
        self.assertEqual(cancelled.module_state, ModuleState.ACTIVE)
        self.assertEqual(cancelled.state_version, 6)

        await coordinator.submit(
            self.command(
                NurseryAction.CONFIRM_DELETION,
                expected=6,
                payload={"subject_version": 6},
            )
        )
        pending_delete = await coordinator.submit(
            self.command(
                NurseryAction.CONFIRM_DELETION,
                actor=self.external,
                expected=6,
                payload={"subject_version": 6},
            )
        )
        self.assertEqual(pending_delete.module_state, ModuleState.DELETION_PENDING)
        self.clock.advance(days=31)
        cleaned = await coordinator.submit(
            self.command(NurseryAction.FINALIZE_CLEANUP, expected=7)
        )
        self.assertEqual(cleaned.module_state, ModuleState.NEVER_ENABLED)
        self.assertEqual(cleaned.state_version, 8)

    async def test_illegal_new_action_is_rejected_without_operation_or_snapshot(self):
        self.setup_child(ModuleState.NEVER_ENABLED)
        command = self.command(NurseryAction.PAUSE, expected=0)
        result = await NurseryCoordinator(self.store).submit(command)
        self.assertEqual(result.status, OperationStatus.REJECTED)
        self.assertEqual(result.error_code, "INVALID_STATE_TRANSITION")
        self.assertEqual(self.store.count_rows("nursery_operations"), 0)
        self.assertEqual(self.store.count_rows("nursery_state_snapshots"), 0)
        self.assertEqual(
            self.store.get_state("account", "child")["state_version"], 0
        )

    async def test_completed_replay_survives_later_state_change(self):
        self.setup_child(ModuleState.NEVER_ENABLED)
        coordinator = NurseryCoordinator(self.store)
        command = self.command(NurseryAction.START_DRAFT, expected=0)
        first = await coordinator.submit(command)
        replay = await coordinator.submit(command)
        self.assertEqual(first.status, OperationStatus.COMPLETED)
        self.assertEqual(replay.status, OperationStatus.COMPLETED)
        self.assertTrue(replay.replayed)
        self.assertEqual(replay.result, first.result)
        self.assertEqual(self.store.count_rows("nursery_operations"), 1)

    async def test_twenty_concurrent_identical_requests_make_one_operation_and_result(self):
        self.setup_child(ModuleState.ACTIVE)

        async def slow_once(command, before):
            await asyncio.sleep(0.05)

        coordinator = NurseryCoordinator(
            self.store,
            queue_config=QueueConfig(lock_wait_seconds=1, replay_wait_seconds=1),
            work_hook=slow_once,
        )
        command = self.command(NurseryAction.PAUSE, expected=0)
        results = await asyncio.gather(
            *(coordinator.submit(command) for _ in range(20))
        )
        self.assertTrue(
            all(result.status == OperationStatus.COMPLETED for result in results)
        )
        self.assertEqual({json.dumps(r.result, sort_keys=True) for r in results}, {
            json.dumps(results[0].result, sort_keys=True)
        })
        self.assertEqual(self.store.count_rows("nursery_operations"), 1)
        self.assertEqual(self.store.count_rows("nursery_source_events"), 1)
        self.assertEqual(len(self.store.snapshots_for(command.operation_id)), 2)
        self.assertEqual(
            self.store.get_state("account", "child")["state_version"], 1
        )

    async def test_idempotency_payload_conflict_and_operation_scope_collision(self):
        self.setup_child(ModuleState.ACTIVE)
        coordinator = NurseryCoordinator(self.store)
        operation_id = str(uuid.uuid4())
        first = self.command(
            NurseryAction.PAUSE,
            actor=self.user,
            expected=0,
            payload={"reason": "first"},
            operation_id=operation_id,
            idempotency_key="same-key",
        )
        await coordinator.submit(first)
        payload_conflict = self.command(
            NurseryAction.PAUSE,
            actor=self.user,
            expected=0,
            payload={"reason": "different"},
            idempotency_key="same-key",
        )
        conflict = await coordinator.submit(payload_conflict)
        self.assertEqual(conflict.error_code, "IDEMPOTENCY_CONFLICT")

        operation_collision = self.command(
            NurseryAction.PAUSE,
            actor=self.external,
            expected=1,
            operation_id=operation_id,
            idempotency_key="external-key",
        )
        collision = await coordinator.submit(operation_collision)
        self.assertEqual(collision.error_code, "OPERATION_ID_CONFLICT")
        self.assertEqual(self.store.count_rows("nursery_operations"), 1)

    async def test_same_source_version_replays_original_even_with_new_transport_key(self):
        self.setup_child(ModuleState.ACTIVE)
        coordinator = NurseryCoordinator(self.store)
        first = self.command(
            NurseryAction.PAUSE,
            expected=0,
            idempotency_key="transport-one",
            source_id="canonical-mailbox-42",
            source_type=SourceType.MAILBOX_EVENT,
        )
        original = await coordinator.submit(first)
        repeated = self.command(
            NurseryAction.PAUSE,
            expected=0,
            idempotency_key="transport-two",
            source_id="canonical-mailbox-42",
            source_type=SourceType.MAILBOX_EVENT,
        )
        replay = await coordinator.submit(repeated)
        self.assertEqual(original.result, replay.result)
        self.assertTrue(replay.replayed)
        self.assertEqual(replay.operation_id, first.operation_id)
        self.assertEqual(self.store.count_rows("nursery_operations"), 1)
        self.assertEqual(self.store.count_rows("nursery_source_events"), 1)

    async def test_old_expected_version_rejects_before_snapshot_and_state_change(self):
        self.setup_child(ModuleState.ACTIVE)
        command = self.command(NurseryAction.PAUSE, expected=99)
        result = await NurseryCoordinator(self.store).submit(command)
        self.assertEqual(result.status, OperationStatus.REJECTED)
        self.assertEqual(result.error_code, "STATE_VERSION_CONFLICT")
        self.assertEqual(self.store.snapshots_for(command.operation_id), [])
        self.assertEqual(
            self.store.get_state("account", "child")["state_version"], 0
        )

    async def test_lock_timeout_stays_pending_and_read_only_poll_does_not_start_it(self):
        self.setup_child(ModuleState.ACTIVE)
        locks = ChildLockRegistry()
        lock = await locks.get("child")
        await lock.acquire()
        coordinator = NurseryCoordinator(
            self.store,
            locks=locks,
            queue_config=QueueConfig(
                lock_wait_seconds=0.02,
                replay_wait_seconds=0.05,
                poll_interval_seconds=0.005,
            ),
        )
        command = self.command(NurseryAction.PAUSE, expected=0)
        try:
            queued = await coordinator.submit(command)
            self.assertEqual(queued.status, OperationStatus.PENDING)
            for _ in range(10):
                polled = await coordinator.operation_status(command.operation_id)
                self.assertEqual(polled.status, OperationStatus.PENDING)
            self.assertEqual(self.store.snapshots_for(command.operation_id), [])
        finally:
            lock.release()
        resumed = await coordinator.submit(command)
        self.assertEqual(resumed.status, OperationStatus.COMPLETED)
        self.assertEqual(resumed.module_state, ModuleState.PAUSED)

    async def test_same_child_fifo_reads_previous_commit_before_processing_next(self):
        self.setup_child(ModuleState.ACTIVE)
        first_started = asyncio.Event()
        release_first = asyncio.Event()
        observed: list[tuple[str, int]] = []

        async def hook(command, before):
            observed.append((command.actor.caregiver_id, before["state_version"]))
            if command.actor.caregiver_id == self.user.caregiver_id:
                first_started.set()
                await release_first.wait()

        coordinator = NurseryCoordinator(
            self.store,
            queue_config=QueueConfig(lock_wait_seconds=1, replay_wait_seconds=1),
            work_hook=hook,
        )
        first = self.command(NurseryAction.PAUSE, actor=self.user, expected=0)
        second = self.command(NurseryAction.PAUSE, actor=self.external, expected=None)
        first_task = asyncio.create_task(coordinator.submit(first))
        await asyncio.wait_for(first_started.wait(), timeout=1)
        second_task = asyncio.create_task(coordinator.submit(second))
        await asyncio.sleep(0.03)
        release_first.set()
        first_result, second_result = await asyncio.gather(first_task, second_task)
        self.assertEqual(first_result.status, OperationStatus.COMPLETED)
        self.assertEqual(second_result.status, OperationStatus.COMPLETED)
        self.assertEqual(observed, [("user-guardian", 0), ("external-ai", 1)])
        self.assertEqual(
            self.store.snapshots_for(second.operation_id)[0]["state_version"], 1
        )

    async def test_different_children_reach_external_work_concurrently(self):
        self.setup_child(ModuleState.ACTIVE)
        second_user = guardian(
            "account-2", "user-2", CaregiverRole.USER_GUARDIAN
        )
        self.store.create_fixture(
            account_id="account-2", child_id="child-2", state=ModuleState.ACTIVE
        )
        self.store.register_caregiver(second_user)
        reached: set[str] = set()
        both = asyncio.Event()

        async def barrier(command, before):
            reached.add(command.child_id)
            if len(reached) == 2:
                both.set()
            await asyncio.wait_for(both.wait(), timeout=1)

        coordinator = NurseryCoordinator(
            self.store,
            queue_config=QueueConfig(lock_wait_seconds=1, replay_wait_seconds=1),
            work_hook=barrier,
        )
        one = self.command(NurseryAction.PAUSE, actor=self.user, expected=0)
        two = self.command(
            NurseryAction.PAUSE,
            actor=second_user,
            child_id="child-2",
            expected=0,
        )
        results = await asyncio.gather(coordinator.submit(one), coordinator.submit(two))
        self.assertEqual(reached, {"child", "child-2"})
        self.assertTrue(all(r.status == OperationStatus.COMPLETED for r in results))

    async def test_external_work_holds_no_sqlite_write_transaction(self):
        self.setup_child(ModuleState.ACTIVE)

        async def assert_database_available(command, before):
            connection = sqlite3.connect(self.db_path, timeout=0.2, isolation_level=None)
            try:
                connection.execute("BEGIN IMMEDIATE")
                connection.rollback()
            finally:
                connection.close()

        coordinator = NurseryCoordinator(self.store, work_hook=assert_database_available)
        result = await coordinator.submit(
            self.command(NurseryAction.PAUSE, expected=0)
        )
        self.assertEqual(result.status, OperationStatus.COMPLETED)

    async def test_mailbox_write_event_is_immediate_and_has_no_silence_gate(self):
        self.setup_child(ModuleState.ACTIVE)
        reached = asyncio.Event()

        async def observe_immediate_event(command, before):
            self.assertEqual(command.source.source_type, SourceType.MAILBOX_EVENT)
            self.assertNotIn("static_ready", command.payload)
            reached.set()

        command = self.command(
            NurseryAction.PAUSE,
            expected=0,
            source_type=SourceType.MAILBOX_EVENT,
            payload={"mailbox_write_status": "completed"},
        )
        result = await NurseryCoordinator(
            self.store, work_hook=observe_immediate_event
        ).submit(command)
        self.assertTrue(reached.is_set())
        self.assertEqual(result.status, OperationStatus.COMPLETED)
        self.assertEqual(result.module_state, ModuleState.PAUSED)

    async def test_stale_processing_requires_explicit_same_key_resume(self):
        self.setup_child(ModuleState.ACTIVE)
        command = self.command(NurseryAction.PAUSE, expected=0)
        registration = self.store.register_operation(command, request_hash(command))
        self.assertEqual(registration["decision"], "new")
        claimed = self.store.claim_operation(
            command.operation_id, lease_owner="dead-worker", lease_seconds=1
        )
        self.assertEqual(claimed["decision"], "claimed")
        self.clock.advance(seconds=2)

        status_only = await NurseryCoordinator(self.store).operation_status(
            command.operation_id
        )
        self.assertEqual(status_only.status, OperationStatus.PROCESSING)
        self.assertEqual(
            self.store.get_state("account", "child")["state_version"], 0
        )

        resumed = await NurseryCoordinator(self.store).submit(
            command, resume_retryable=True
        )
        self.assertEqual(resumed.status, OperationStatus.COMPLETED)
        self.assertTrue(resumed.replayed)
        self.assertEqual(len(self.store.snapshots_for(command.operation_id)), 2)
        self.assertEqual(self.store.count_rows("nursery_operations"), 1)

    async def test_rule_failure_is_retryable_and_resume_does_not_duplicate_before(self):
        self.setup_child(ModuleState.ACTIVE)

        async def broken(command, before):
            raise RuntimeError("do not persist this message")

        command = self.command(NurseryAction.PAUSE, expected=0)
        failed = await NurseryCoordinator(self.store, work_hook=broken).submit(command)
        self.assertEqual(failed.status, OperationStatus.FAILED_RETRYABLE)
        self.assertEqual(failed.error_code, "RULE_EXECUTION_FAILED")
        self.assertEqual(len(self.store.snapshots_for(command.operation_id)), 1)
        self.assertEqual(
            self.store.get_state("account", "child")["state_version"], 0
        )

        replay_only = await NurseryCoordinator(self.store).submit(command)
        self.assertEqual(replay_only.status, OperationStatus.FAILED_RETRYABLE)
        self.assertTrue(replay_only.replayed)
        self.assertEqual(
            self.store.get_state("account", "child")["state_version"], 0
        )

        resumed = await NurseryCoordinator(self.store).submit(
            command, resume_retryable=True
        )
        self.assertEqual(resumed.status, OperationStatus.COMPLETED)
        snapshots = self.store.snapshots_for(command.operation_id)
        self.assertEqual([item["snapshot_kind"] for item in snapshots], ["before", "after"])

    async def test_failed_retryable_keeps_fifo_head_until_explicit_resolution(self):
        self.setup_child(ModuleState.ACTIVE)

        async def broken(command, before):
            raise RuntimeError("first operation failed")

        first = self.command(NurseryAction.PAUSE, actor=self.user, expected=0)
        failed = await NurseryCoordinator(self.store, work_hook=broken).submit(first)
        self.assertEqual(failed.status, OperationStatus.FAILED_RETRYABLE)

        second = self.command(
            NurseryAction.PAUSE,
            actor=self.external,
            expected=None,
        )
        queued = await NurseryCoordinator(self.store).submit(second)
        self.assertEqual(queued.status, OperationStatus.PENDING)
        self.assertEqual(self.store.snapshots_for(second.operation_id), [])

        recovered = await NurseryCoordinator(self.store).submit(
            first, resume_retryable=True
        )
        self.assertEqual(recovered.status, OperationStatus.COMPLETED)
        later = await NurseryCoordinator(self.store).submit(second)
        self.assertEqual(later.status, OperationStatus.COMPLETED)
        self.assertEqual(
            self.store.snapshots_for(second.operation_id)[0]["state_version"], 1
        )

    async def test_version_change_after_before_anchor_cannot_overwrite_new_state(self):
        self.setup_child(ModuleState.ACTIVE)

        async def competing_commit(command, before):
            with closing(sqlite3.connect(self.db_path)) as connection:
                connection.execute(
                    "UPDATE nursery_children SET state_version=1 WHERE child_id='child'"
                )
                connection.execute(
                    "UPDATE nursery_modules SET state_version=1 WHERE child_id='child'"
                )
                connection.commit()

        command = self.command(NurseryAction.PAUSE, expected=0)
        result = await NurseryCoordinator(
            self.store, work_hook=competing_commit
        ).submit(command)
        self.assertEqual(result.status, OperationStatus.FAILED_RETRYABLE)
        self.assertEqual(result.error_code, "STATE_VERSION_CHANGED")
        state = self.store.get_state("account", "child")
        self.assertEqual(state["module_state"], ModuleState.ACTIVE.value)
        self.assertEqual(state["state_version"], 1)
        self.assertEqual(len(self.store.snapshots_for(command.operation_id)), 1)

    async def test_task_cancellation_releases_lock_and_records_cancelled(self):
        self.setup_child(ModuleState.ACTIVE)
        entered = asyncio.Event()
        never = asyncio.Event()
        locks = ChildLockRegistry()

        async def wait_forever(command, before):
            entered.set()
            await never.wait()

        command = self.command(NurseryAction.PAUSE, expected=0)
        coordinator = NurseryCoordinator(self.store, locks=locks, work_hook=wait_forever)
        task = asyncio.create_task(coordinator.submit(command))
        await asyncio.wait_for(entered.wait(), timeout=1)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        operation = self.store.get_operation(command.operation_id)
        self.assertEqual(operation["status"], OperationStatus.CANCELLED.value)
        self.assertFalse((await locks.get("child")).locked())
        self.assertEqual(
            self.store.get_state("account", "child")["state_version"], 0
        )
        replay = await NurseryCoordinator(self.store).submit(command)
        self.assertEqual(replay.status, OperationStatus.CANCELLED)
        self.assertTrue(replay.replayed)

    async def test_rejected_operation_replays_without_new_snapshot_or_version(self):
        self.setup_child(ModuleState.ACTIVE)
        command = self.command(NurseryAction.PAUSE, expected=50)
        coordinator = NurseryCoordinator(self.store)
        first = await coordinator.submit(command)
        replay = await coordinator.submit(command)
        self.assertEqual(first.status, OperationStatus.REJECTED)
        self.assertEqual(replay.status, OperationStatus.REJECTED)
        self.assertTrue(replay.replayed)
        self.assertEqual(replay.error_code, "STATE_VERSION_CONFLICT")
        self.assertEqual(self.store.snapshots_for(command.operation_id), [])
        self.assertEqual(self.store.count_rows("nursery_operations"), 1)

    async def test_revoking_old_source_does_not_delete_later_valid_operation(self):
        self.setup_child(ModuleState.ACTIVE)
        coordinator = NurseryCoordinator(self.store)
        pause = self.command(NurseryAction.PAUSE, expected=0)
        resume = self.command(NurseryAction.RESUME, expected=1)
        await coordinator.submit(pause)
        await coordinator.submit(resume)
        revoked = self.store.mark_source_revoked(
            account_id="account",
            child_id="child",
            source_type=pause.source.source_type.value,
            source_id=pause.source.source_id,
            source_version=pause.source.source_version,
        )
        self.assertEqual(revoked["processing_status"], "revoked")
        self.assertIsNotNone(self.store.get_operation(resume.operation_id))
        self.assertEqual(len(self.store.snapshots_for(resume.operation_id)), 2)
        self.assertEqual(
            self.store.get_state("account", "child")["module_state"],
            ModuleState.ACTIVE.value,
        )

    async def test_success_has_actor_source_before_after_and_new_version_trace(self):
        self.setup_child(ModuleState.ACTIVE)
        command = self.command(NurseryAction.PAUSE, expected=0)
        result = await NurseryCoordinator(self.store).submit(command)
        operation = self.store.get_operation(command.operation_id)
        source_row = self.store.source_for(command.operation_id)
        snapshots = self.store.snapshots_for(command.operation_id)
        self.assertEqual(result.status, OperationStatus.COMPLETED)
        self.assertEqual(operation["caregiver_id"], self.user.caregiver_id)
        self.assertEqual(source_row["processing_status"], "applied")
        self.assertEqual(source_row["source_id"], command.source.source_id)
        self.assertEqual(
            [(item["snapshot_kind"], item["state_version"]) for item in snapshots],
            [("before", 0), ("after", 1)],
        )

    async def test_non_guardian_cannot_create_an_operation(self):
        self.setup_child(ModuleState.ACTIVE)
        companion = ActorContext.build(
            account_id="account",
            caregiver_id="companion",
            role=CaregiverRole.COMPANION,
            permissions=["nursery.*"],
            auth_source=AuthSource.TEST_FIXTURE,
        )
        self.store.register_caregiver(companion)
        command = self.command(NurseryAction.PAUSE, actor=companion, expected=0)
        with self.assertRaises(NurseryError) as caught:
            await NurseryCoordinator(self.store).submit(command)
        self.assertEqual(caught.exception.code, "ACTOR_NOT_GUARDIAN")
        self.assertEqual(self.store.count_rows("nursery_operations"), 0)


if __name__ == "__main__":
    unittest.main()
