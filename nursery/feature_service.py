"""Post-creation nursery actions through the shared coordinator."""

from __future__ import annotations

import uuid
from typing import Any

from .body_state import BodyTimeRule
from .capability_rules import CapabilityRules, validate_growth_observation
from .coordinator import NurseryCoordinator
from .models import (
    ActorContext,
    NurseryAction,
    NurseryCommand,
    NurseryError,
    SourceReference,
)
from .store import NurseryStore


USER_FEATURE_ACTIONS = frozenset(
    {
        NurseryAction.ADD_AREA,
        NurseryAction.UPDATE_AREA,
        NurseryAction.ADD_ITEM,
        NurseryAction.MOVE_ITEM,
        NurseryAction.UPDATE_ITEM,
        NurseryAction.STORE_ITEM,
        NurseryAction.REMOVE_ITEM,
        NurseryAction.PROPOSE_NAME_CHANGE,
        NurseryAction.CONFIRM_NAME_CHANGE,
        NurseryAction.WITHDRAW_NAME_CHANGE,
        NurseryAction.UPDATE_OWN_CALLING_PREFERENCE,
        NurseryAction.RECORD_GROWTH_OBSERVATION,
        NurseryAction.PROPOSE_STAGE,
        NurseryAction.CONFIRM_STAGE,
        NurseryAction.WITHDRAW_STAGE_PROPOSAL,
        NurseryAction.RECORD_CARE_OBSERVATION,
        NurseryAction.COMFORT_CARE,
        NurseryAction.SET_REST_STATE,
        NurseryAction.CORRECT_CARE_EVENT,
        NurseryAction.UPDATE_FAMILY_THREAD,
        NurseryAction.RECORD_RELATIONSHIP_EVENT,
        NurseryAction.SAVE_CONFIRMED_CARE_PLAN,
        NurseryAction.EXECUTE_CONFIRMED_CARE,
        NurseryAction.PAUSE,
        NurseryAction.RESUME,
        NurseryAction.REQUEST_DELETION,
        NurseryAction.CONFIRM_DELETION,
        NurseryAction.CANCEL_DELETION,
    }
)


class NurseryFeatureService:
    """Minimal facade used by both the phone and personal MCP adapters."""

    def __init__(
        self, store: NurseryStore, *, body_time_rule: BodyTimeRule | None = None,
        capability_rules: CapabilityRules | None = None,
    ) -> None:
        self.store = store
        self.capability_rules = capability_rules
        self.coordinator = NurseryCoordinator(store, body_time_rule=body_time_rule, prepare_hook=self._prepare)

    def _prepare(self, command: NurseryCommand, before: dict[str, Any]) -> None:
        if command.action == NurseryAction.RECORD_GROWTH_OBSERVATION:
            validate_growth_observation(command.payload, self.capability_rules, str(before["stage_id"]))

    async def submit(
        self,
        *,
        actor: ActorContext,
        child_id: str,
        action: NurseryAction,
        payload: dict[str, Any],
        source: SourceReference,
        operation_id: str | None = None,
        idempotency_key: str | None = None,
        expected_state_version: int | None = None,
        resume_retryable: bool = False,
    ) -> dict[str, Any]:
        if action not in USER_FEATURE_ACTIONS:
            raise NurseryError(
                "ACTION_NOT_EXPOSED",
                "this action is not available through the nursery feature adapter",
            )
        operation = str(operation_id or "").strip() or str(uuid.uuid4())
        idempotency = str(idempotency_key or "").strip() or operation
        result = await self.coordinator.submit(
            NurseryCommand(
                actor=actor,
                child_id=str(child_id).strip(),
                action=action,
                operation_id=operation,
                idempotency_key=idempotency,
                expected_state_version=expected_state_version,
                payload=dict(payload),
                source=source,
            ),
            resume_retryable=resume_retryable,
        )
        return result.as_dict()
