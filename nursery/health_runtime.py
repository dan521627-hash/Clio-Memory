"""Post-commit health evaluation for real nursery writes only.

The default is a fixed ``no_change`` rule.  It records an auditable decision
but cannot manufacture a health or body-state change.
"""

from __future__ import annotations

from typing import Any

from .health_contract import FixedHealthRule, HealthEvaluationService
from .models import (
    HealthOutcome,
    HealthTrigger,
    NurseryAction,
    NurseryError,
    OperationStatus,
    SourceReference,
    SourceType,
)
from .store import NurseryStore


class NurseryHealthRuntime:
    """Run the existing health contract only for committed eligible writes."""

    def __init__(
        self,
        store: NurseryStore,
        *,
        evaluator: HealthEvaluationService | None = None,
    ) -> None:
        self.store = store
        self.evaluator = evaluator or HealthEvaluationService(
            store,
            FixedHealthRule(
                outcome=HealthOutcome.NO_CHANGE,
                rule_version="health-runtime-no-change-v1",
            ),
        )

    def _require_committed_source(
        self,
        *,
        account_id: str,
        child_id: str,
        source: SourceReference,
        operation_id: str,
        action: NurseryAction | tuple[NurseryAction, ...],
    ) -> None:
        """Accept an evaluation only after its exact source write committed."""

        operation = self.store.get_operation(operation_id)
        source_row = self.store.source_for(operation_id)
        if (
            not operation
            or not source_row
            or operation["account_id"] != account_id
            or operation["child_id"] != child_id
            or operation["action"]
            not in {
                item.value
                for item in (action if isinstance(action, tuple) else (action,))
            }
            or operation["status"] != OperationStatus.COMPLETED.value
            or source_row["processing_status"] != "applied"
            or source_row["source_type"] != source.source_type.value
            or source_row["source_id"] != source.source_id
            or source_row["source_version"] != source.source_version
        ):
            raise NurseryError(
                "HEALTH_COMMITTED_SOURCE_REQUIRED",
                "health evaluation requires its completed source operation",
            )

    def _conditions(
        self, *, account_id: str, child_id: str, activity: str
    ) -> dict[str, Any]:
        state = self.store.get_state(account_id, child_id)
        return {
            "age_stage": str(state["stage_id"]),
            "activity": activity,
        }

    def evaluate_interaction(
        self,
        *,
        account_id: str,
        child_id: str,
        source: SourceReference,
        operation_id: str,
    ) -> dict[str, Any]:
        if source.source_type not in {
            SourceType.USER_INTERACTION,
            SourceType.EXTERNAL_AI_INTERACTION,
        }:
            raise NurseryError(
                "HEALTH_INTERACTION_SOURCE_REQUIRED",
                "health interaction evaluation requires a committed interaction source",
            )
        self._require_committed_source(
            account_id=account_id,
            child_id=child_id,
            source=source,
            operation_id=operation_id,
            action=NurseryAction.CHILD_INTERACT,
        )
        return self.evaluator.evaluate(
            account_id=account_id,
            child_id=child_id,
            trigger=HealthTrigger.INTERACTION,
            source=source,
            conditions=self._conditions(
                account_id=account_id,
                child_id=child_id,
                activity="confirmed_interaction",
            ),
            operation_id=operation_id,
        )

    def evaluate_family_event(
        self,
        *,
        account_id: str,
        child_id: str,
        source: SourceReference,
        operation_id: str,
        health_relevant: bool,
    ) -> dict[str, Any] | None:
        """Evaluate only a bridge-vetted, relevant mailbox write.

        Other Anima sources and unmarked events are intentionally ignored: a
        family-event reaction is not itself evidence for a health evaluation.
        """

        if health_relevant is not True:
            return None
        if source.source_type != SourceType.MAILBOX_EVENT:
            raise NurseryError(
                "HEALTH_MAILBOX_SOURCE_REQUIRED",
                "health family-event evaluation requires a relevant mailbox source",
            )
        self._require_committed_source(
            account_id=account_id,
            child_id=child_id,
            source=source,
            operation_id=operation_id,
            action=(
                NurseryAction.APPLY_ANIMA_FAMILY_EVENT,
                # Compatibility for context-only bridge writes committed before
                # the immediate family-event action existed.
                NurseryAction.SYNC_ANIMA_CONTEXT,
            ),
        )
        return self.evaluator.evaluate(
            account_id=account_id,
            child_id=child_id,
            trigger=HealthTrigger.RELEVANT_MAILBOX_EVENT,
            source=source,
            conditions=self._conditions(
                account_id=account_id,
                child_id=child_id,
                activity="relevant_mailbox_event",
            ),
            operation_id=operation_id,
        )


def evaluate_family_event_health(
    *,
    store: NurseryStore,
    account_id: str,
    child_id: str,
    source: SourceReference,
    operation_id: str,
    health_relevant: bool,
) -> dict[str, Any] | None:
    """Small bridge-facing wrapper; callers invoke it only after commit."""

    return NurseryHealthRuntime(store).evaluate_family_event(
        account_id=account_id,
        child_id=child_id,
        source=source,
        operation_id=operation_id,
        health_relevant=health_relevant,
    )
