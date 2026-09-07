"""Single stage-1 coordinator for ordering, idempotency, and state commits."""

from __future__ import annotations

import asyncio
import inspect
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from .body_state import BodyTimeRule
from .models import (
    AuthSource,
    CaregiverRole,
    ModuleState,
    NurseryAction,
    NurseryCommand,
    NurseryError,
    OperationResult,
    OperationStatus,
    SafetyCategory,
    TERMINAL_OPERATION_STATUSES,
    ensure_action_allowed,
)
from .operations import (
    request_hash,
    validate_idempotency_key,
    validate_operation_id,
)
from .queue import ChildLockRegistry, QueueConfig
from .safety import validate_actor, validate_public_operation_payload
from .store import NurseryStore


WorkHook = Callable[[NurseryCommand, dict[str, Any]], Awaitable[None] | None]
PreparationCallback = Callable[[dict[str, Any]], Awaitable[None] | None]


@dataclass
class PreparedMutation:
    """Ephemeral material prepared after claim and never serialized as an operation."""

    data: dict[str, Any] = field(default_factory=dict)
    on_commit: PreparationCallback | None = None
    on_abort: PreparationCallback | None = None


PrepareHook = Callable[
    [NurseryCommand, dict[str, Any]],
    Awaitable[PreparedMutation | dict[str, Any] | None]
    | PreparedMutation
    | dict[str, Any]
    | None,
]


class NurseryCoordinator:
    """The only supported mutation path above :class:`NurseryStore`."""

    def __init__(
        self,
        store: NurseryStore,
        *,
        queue_config: QueueConfig | None = None,
        locks: ChildLockRegistry | None = None,
        work_hook: WorkHook | None = None,
        prepare_hook: PrepareHook | None = None,
        body_time_rule: BodyTimeRule | None = None,
    ) -> None:
        self.store = store
        self.queue_config = queue_config or QueueConfig()
        self.locks = locks or ChildLockRegistry()
        self.work_hook = work_hook
        self.prepare_hook = prepare_hook
        self.body_time_rule = body_time_rule or BodyTimeRule()
        try:
            self.body_time_rule.validate()
        except ValueError as exc:
            raise NurseryError("INVALID_BODY_RULE", str(exc)) from exc

    @staticmethod
    def _validate_command(command: NurseryCommand) -> None:
        if not isinstance(command, NurseryCommand):
            raise NurseryError("INVALID_COMMAND", "NurseryCommand is required")
        validate_operation_id(command.operation_id)
        validate_idempotency_key(command.idempotency_key)
        if not str(command.child_id).strip():
            raise NurseryError("INVALID_CHILD_ID", "child_id is required")
        if (
            command.expected_state_version is not None
            and (
                type(command.expected_state_version) is not int
                or command.expected_state_version < 0
            )
        ):
            raise NurseryError(
                "INVALID_STATE_VERSION",
                "expected_state_version must be a non-negative integer or null",
            )
        if command.source is None:
            raise NurseryError(
                "SOURCE_REQUIRED", "every nursery mutation requires a source reference"
            )
        validate_public_operation_payload(command.payload)
        validate_actor(command.actor, command.action)

    @staticmethod
    def _from_operation(
        operation: dict[str, Any],
        *,
        replayed: bool,
    ) -> OperationResult:
        status = OperationStatus(operation["status"])
        payload = operation.get("result") or {}
        state_value = payload.get("module_state")
        return OperationResult(
            operation_id=str(operation["operation_id"]),
            status=status,
            replayed=replayed,
            changed=bool(payload.get("changed", False)),
            module_state=ModuleState(state_value) if state_value else None,
            state_version=(
                int(payload["state_version"])
                if payload.get("state_version") is not None
                else operation.get("before_state_version")
            ),
            result=dict(payload),
            error_code=str(operation.get("error_code") or ""),
            error_message=str(operation.get("error_message") or ""),
        )

    async def _store_call(self, function: Callable[..., Any], *args, **kwargs):
        return await asyncio.to_thread(function, *args, **kwargs)

    @staticmethod
    async def _run_preparation_callback(
        callback: PreparationCallback | None,
        operation: dict[str, Any],
    ) -> None:
        if callback is None:
            return
        try:
            maybe_awaitable = callback(operation)
            if inspect.isawaitable(maybe_awaitable):
                await maybe_awaitable
        except Exception:
            # A committed database result must not be rewritten by vault cleanup.
            return

    async def _wait_for_settled(
        self,
        operation_id: str,
        *,
        replayed: bool,
    ) -> OperationResult:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self.queue_config.replay_wait_seconds
        latest: dict[str, Any] | None = None
        while loop.time() < deadline:
            latest = await self._store_call(self.store.get_operation, operation_id)
            if latest and OperationStatus(latest["status"]) in (
                TERMINAL_OPERATION_STATUSES | {OperationStatus.FAILED_RETRYABLE}
            ):
                return self._from_operation(latest, replayed=replayed)
            await asyncio.sleep(self.queue_config.poll_interval_seconds)
        latest = latest or await self._store_call(
            self.store.get_operation, operation_id
        )
        if latest:
            return self._from_operation(latest, replayed=replayed)
        raise NurseryError("OPERATION_NOT_FOUND", "operation disappeared while waiting")

    async def submit(
        self,
        command: NurseryCommand,
        *,
        resume_retryable: bool = False,
    ) -> OperationResult:
        """Submit or explicitly resume one nursery command.

        Polling/query methods never call this function, so reads cannot start work.
        """

        self._validate_command(command)
        try:
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
                await self._store_call(self.store.assert_actor_registered, command.actor)
            state = await self._store_call(
                self.store.get_state, command.actor.account_id, command.child_id
            )
        except NurseryError as exc:
            await self._audit(command, exc, SafetyCategory.AUTHORIZATION)
            raise

        digest = request_hash(command)
        try:
            registration = await self._store_call(
                self.store.register_operation, command, digest
            )
        except NurseryError as exc:
            await self._audit(command, exc, SafetyCategory.STATE)
            return OperationResult(
                operation_id=command.operation_id,
                status=OperationStatus.REJECTED,
                module_state=ModuleState(state["module_state"]),
                state_version=int(state["state_version"]),
                error_code=exc.code,
                error_message=exc.message,
            )
        if registration["decision"] == "conflict":
            code = registration["code"]
            exc = NurseryError(code, "operation identity conflicts with saved request")
            await self._audit(command, exc, SafetyCategory.IDEMPOTENCY)
            return OperationResult(
                operation_id=command.operation_id,
                status=OperationStatus.REJECTED,
                replayed=False,
                module_state=ModuleState(state["module_state"]),
                state_version=int(state["state_version"]),
                error_code=code,
                error_message=exc.message,
            )

        operation = registration["operation"]
        replayed = registration["decision"] == "replay"
        status = OperationStatus(operation["status"])
        if status in TERMINAL_OPERATION_STATUSES:
            return self._from_operation(operation, replayed=True)
        if status == OperationStatus.FAILED_RETRYABLE:
            if not resume_retryable:
                return self._from_operation(operation, replayed=True)
            resumed = await self._store_call(
                self.store.prepare_explicit_resume, operation["operation_id"]
            )
            operation = resumed["operation"]
            if resumed["decision"] not in {"resumed", "unchanged"}:
                return self._from_operation(operation, replayed=True)
        elif status == OperationStatus.PROCESSING and resume_retryable:
            resumed = await self._store_call(
                self.store.prepare_explicit_resume, operation["operation_id"]
            )
            operation = resumed["operation"]
            if resumed["decision"] == "busy":
                return await self._wait_for_settled(
                    operation["operation_id"], replayed=True
                )

        child_lock = await self.locks.get(command.child_id)
        try:
            await asyncio.wait_for(
                child_lock.acquire(), timeout=self.queue_config.lock_wait_seconds
            )
        except TimeoutError:
            latest = await self._store_call(
                self.store.get_operation, operation["operation_id"]
            )
            if latest and OperationStatus(latest["status"]) in (
                TERMINAL_OPERATION_STATUSES | {OperationStatus.FAILED_RETRYABLE}
            ):
                return self._from_operation(latest, replayed=replayed)
            # A duplicate request can time out on the process-local child lock
            # while the first caller is still testing a model connection.  Do
            # not expose that transient ``processing`` row as the replay result;
            # give the already-owned operation the normal replay settlement
            # window first.
            return await self._wait_for_settled(
                operation["operation_id"], replayed=True
            )

        lease_owner = str(uuid.uuid4())
        claimed = False
        preparation: PreparedMutation | None = None
        try:
            claim = await self._store_call(
                self.store.claim_operation,
                operation["operation_id"],
                lease_owner=lease_owner,
                lease_seconds=self.queue_config.lease_seconds,
            )
            if claim["decision"] in {"rejected", "completed"}:
                return self._from_operation(claim["operation"], replayed=replayed)
            if claim["decision"] == "queued":
                # A care execution is an idempotent statement of one actual
                # event, so a simultaneous second caregiver should receive the
                # first executor's record instead of an orphaned pending row.
                # Other actions retain the normal non-blocking FIFO contract.
                if command.action != NurseryAction.EXECUTE_CONFIRMED_CARE:
                    return self._from_operation(claim["operation"], replayed=replayed)
                await self._wait_for_settled(
                    str(claim["blocked_by"]), replayed=True
                )
                claim = await self._store_call(
                    self.store.claim_operation,
                    operation["operation_id"],
                    lease_owner=lease_owner,
                    lease_seconds=self.queue_config.lease_seconds,
                )
                if claim["decision"] in {"rejected", "completed"}:
                    return self._from_operation(claim["operation"], replayed=replayed)
                if claim["decision"] == "queued":
                    return self._from_operation(claim["operation"], replayed=replayed)
            if claim["decision"] != "claimed":
                return await self._wait_for_settled(
                    operation["operation_id"], replayed=True
                )
            claimed = True

            current = ModuleState(claim["before"]["module_state"])
            ensure_action_allowed(current, command.action)

            if self.work_hook is not None:
                maybe_awaitable = self.work_hook(command, dict(claim["before"]))
                if inspect.isawaitable(maybe_awaitable):
                    await maybe_awaitable

            if self.prepare_hook is not None:
                prepared_value = self.prepare_hook(command, dict(claim["before"]))
                if inspect.isawaitable(prepared_value):
                    prepared_value = await prepared_value
                if isinstance(prepared_value, PreparedMutation):
                    preparation = prepared_value
                elif isinstance(prepared_value, dict):
                    preparation = PreparedMutation(data=prepared_value)
                elif prepared_value is not None:
                    raise NurseryError(
                        "INVALID_PREPARATION",
                        "prepare hook returned an unsupported value",
                    )

            # Time is only settled on a successful real write.  Interactions
            # prepare their own projection before the model sees it.
            if (
                command.action != NurseryAction.CHILD_INTERACT
                and self.body_time_rule.enabled
                and current == ModuleState.ACTIVE
            ):
                settlement = await self._store_call(
                    self.store.preview_body_settlement,
                    child_id=command.child_id,
                    through=self.store.clock(),
                    rule=self.body_time_rule,
                )
                if preparation is None:
                    preparation = PreparedMutation()
                if "body_settlement" in preparation.data:
                    raise NurseryError(
                        "INVALID_PREPARATION",
                        "body settlement was prepared more than once",
                    )
                preparation.data["body_settlement"] = settlement

            completed = await self._store_call(
                self.store.apply_claimed_operation,
                operation["operation_id"],
                lease_owner=lease_owner,
                prepared=(preparation.data if preparation else None),
            )
            if OperationStatus(completed["status"]) == OperationStatus.COMPLETED:
                await self._run_preparation_callback(
                    preparation.on_commit if preparation else None,
                    completed,
                )
            else:
                await self._run_preparation_callback(
                    preparation.on_abort if preparation else None,
                    completed,
                )
            return self._from_operation(completed, replayed=replayed)
        except asyncio.CancelledError:
            await self._run_preparation_callback(
                preparation.on_abort if preparation else None,
                {"operation_id": operation["operation_id"], "status": "cancelled"},
            )
            if claimed:
                await asyncio.shield(
                    self._store_call(
                        self.store.cancel_operation, operation["operation_id"]
                    )
                )
            raise
        except NurseryError as exc:
            await self._run_preparation_callback(
                preparation.on_abort if preparation else None,
                {"operation_id": operation["operation_id"], "status": "rejected"},
            )
            if claimed:
                rejected = await self._store_call(
                    self.store.reject_operation,
                    operation["operation_id"],
                    error_code=exc.code,
                    error_message=exc.message,
                )
                await self._audit(command, exc, SafetyCategory.STATE)
                return self._from_operation(rejected, replayed=replayed)
            raise
        except Exception as exc:
            await self._run_preparation_callback(
                preparation.on_abort if preparation else None,
                {
                    "operation_id": operation["operation_id"],
                    "status": "failed_retryable",
                },
            )
            if claimed:
                failed = await self._store_call(
                    self.store.fail_operation_retryable,
                    operation["operation_id"],
                    error_code="RULE_EXECUTION_FAILED",
                    error_message=type(exc).__name__,
                )
                return self._from_operation(failed, replayed=replayed)
            raise
        finally:
            child_lock.release()

    async def _audit(
        self,
        command: NurseryCommand,
        error: NurseryError,
        category: SafetyCategory,
    ) -> None:
        try:
            await self._store_call(
                self.store.record_safety_audit,
                category,
                account_id=command.actor.account_id,
                child_id=command.child_id,
                caregiver_id=command.actor.caregiver_id,
                operation_id=command.operation_id,
                error_code=error.code,
                metadata={"action": command.action.value, "reason": error.code},
            )
        except Exception:
            # Safety logging must not replace the original stable error.
            return

    async def operation_status(self, operation_id: str) -> OperationResult:
        """Read only: never claims, resumes, or starts a pending operation."""

        operation = await self._store_call(self.store.get_operation, operation_id)
        if not operation:
            raise NurseryError("OPERATION_NOT_FOUND", "operation was not found")
        return self._from_operation(operation, replayed=True)
