"""Stage-1 health contract: deterministic validation, no illness generation."""

from __future__ import annotations

import hashlib
import threading
from dataclasses import dataclass
from datetime import timedelta
from typing import Any, Protocol

from .models import (
    HealthOutcome,
    HealthTrigger,
    ModuleState,
    NurseryError,
    SafetyCategory,
    SourceReference,
)
from .operations import canonical_json
from .store import NurseryStore, parse_time


ALLOWED_CONDITION_KEYS = frozenset(
    {
        "age_stage",
        "activity",
        "room_environment",
        "confirmed_interaction",
        "long_term_stress",
        "protective_factors",
        "existing_health_event",
    }
)
ALLOWED_TRIGGERS = frozenset(
    {
        HealthTrigger.VALID_WAKE,
        HealthTrigger.INTERACTION,
        HealthTrigger.RELEVANT_MAILBOX_EVENT,
    }
)
FORBIDDEN_OUTCOMES = frozenset(
    {
        "severe_illness",
        "death",
        "permanent_disability",
        "serious_injury",
        "diagnosis",
        "medication",
        "dosage",
        "frequent_illness",
    }
)
ACTIVE_HEALTH_OUTCOMES = frozenset(
    {
        HealthOutcome.OBSERVE.value,
        HealthOutcome.MILD_ILLNESS_CANDIDATE.value,
        HealthOutcome.MILD_DISCOMFORT_CANDIDATE.value,
        HealthOutcome.MINOR_INJURY_CANDIDATE.value,
        HealthOutcome.CARE.value,
    }
)


class HealthRule(Protocol):
    rule_version: str

    def evaluate(self, conditions: dict[str, Any]) -> HealthOutcome | str: ...


@dataclass(frozen=True)
class FixedHealthRule:
    """Test-only deterministic rule; never used as a real probability engine."""

    outcome: HealthOutcome | str = HealthOutcome.NO_CHANGE
    rule_version: str = "stage1-fixed-v1"

    def evaluate(self, conditions: dict[str, Any]) -> HealthOutcome | str:
        return self.outcome


@dataclass(frozen=True)
class HealthGuardConfig:
    """Numeric guards are disabled until explicitly supplied by a later stage."""

    max_evaluations_per_window: int | None = None
    window_seconds: float | None = None
    cooldown_seconds: float | None = None
    block_when_active_event: bool = True

    def __post_init__(self) -> None:
        if (self.max_evaluations_per_window is None) != (self.window_seconds is None):
            raise ValueError("max_evaluations_per_window and window_seconds pair together")
        if self.max_evaluations_per_window is not None:
            if self.max_evaluations_per_window <= 0 or self.window_seconds <= 0:
                raise ValueError("health frequency guard values must be positive")
        if self.cooldown_seconds is not None and self.cooldown_seconds <= 0:
            raise ValueError("cooldown_seconds must be positive")


class HealthEvaluationService:
    def __init__(
        self,
        store: NurseryStore,
        rule: HealthRule,
        *,
        guard: HealthGuardConfig | None = None,
    ) -> None:
        self.store = store
        self.rule = rule
        self.guard = guard or HealthGuardConfig()
        self._evaluation_locks: dict[str, threading.Lock] = {}
        self._evaluation_locks_guard = threading.Lock()

    def _lock_for(self, evaluation_key: str) -> threading.Lock:
        with self._evaluation_locks_guard:
            lock = self._evaluation_locks.get(evaluation_key)
            if lock is None:
                lock = threading.Lock()
                self._evaluation_locks[evaluation_key] = lock
            return lock

    @staticmethod
    def _validate_conditions(conditions: dict[str, Any]) -> str:
        if not isinstance(conditions, dict) or not conditions:
            raise NurseryError(
                "HEALTH_SOURCE_CONDITIONS_REQUIRED",
                "health evaluation requires source-backed conditions",
            )
        unknown = set(conditions) - ALLOWED_CONDITION_KEYS
        if unknown:
            raise NurseryError(
                "HEALTH_CONDITION_NOT_ALLOWED",
                "health conditions contain an unsupported category",
                details={"categories": sorted(unknown)},
            )
        return hashlib.sha256(canonical_json(conditions).encode("utf-8")).hexdigest()

    @staticmethod
    def _evaluation_key(
        *,
        child_id: str,
        source: SourceReference,
        state_version: int,
        rule_version: str,
    ) -> str:
        material = {
            "child_id": child_id,
            "source_type": source.source_type.value,
            "source_id": source.source_id,
            "source_version": source.source_version,
            "state_version": state_version,
            "rule_version": rule_version,
        }
        return hashlib.sha256(canonical_json(material).encode("utf-8")).hexdigest()

    def _check_frequency(self, child_id: str) -> None:
        now = self.store.clock()
        since = None
        if self.guard.window_seconds is not None:
            since = now - timedelta(seconds=self.guard.window_seconds)
        guard_state = self.store.health_guard_state(child_id, since=since)
        if (
            self.guard.max_evaluations_per_window is not None
            and guard_state["count"] >= self.guard.max_evaluations_per_window
        ):
            raise NurseryError(
                "HEALTH_FREQUENCY_GUARD", "health evaluation frequency limit reached"
            )
        latest_at = parse_time(guard_state["latest_at"])
        if (
            self.guard.cooldown_seconds is not None
            and latest_at is not None
            and (now - latest_at).total_seconds() < self.guard.cooldown_seconds
        ):
            raise NurseryError("HEALTH_COOLDOWN", "health evaluation is cooling down")
        if (
            self.guard.block_when_active_event
            and guard_state["latest_outcome"] in ACTIVE_HEALTH_OUTCOMES
        ):
            raise NurseryError(
                "HEALTH_EVENT_ALREADY_ACTIVE",
                "an active health event must be resolved before another is evaluated",
            )

    def evaluate(
        self,
        *,
        account_id: str,
        child_id: str,
        trigger: HealthTrigger | str,
        source: SourceReference,
        conditions: dict[str, Any],
        operation_id: str | None = None,
    ) -> dict[str, Any]:
        event_trigger = HealthTrigger(trigger)
        if not isinstance(source, SourceReference):
            raise NurseryError(
                "HEALTH_SOURCE_REQUIRED",
                "health evaluation requires a canonical source reference",
            )
        if event_trigger not in ALLOWED_TRIGGERS:
            raise NurseryError(
                "HEALTH_TRIGGER_NOT_ALLOWED",
                "reads, page opens, refreshes, and polls cannot run health evaluation",
            )
        state = self.store.get_state(account_id, child_id)
        if ModuleState(state["module_state"]) != ModuleState.ACTIVE:
            raise NurseryError(
                "HEALTH_FROZEN", "health evaluation is frozen outside active state"
            )
        condition_hash = self._validate_conditions(conditions)
        key = self._evaluation_key(
            child_id=child_id,
            source=source,
            state_version=int(state["state_version"]),
            rule_version=str(self.rule.rule_version),
        )
        # The brain is the sole runtime writer. This keyed lock prevents its
        # concurrent retries from evaluating the same canonical event twice;
        # the database UNIQUE key remains the final persistence authority.
        with self._lock_for(key):
            existing = self.store.get_health_evaluation(key)
            if existing:
                return existing
            self._check_frequency(child_id)
            raw_outcome = self.rule.evaluate(dict(conditions))
            raw_value = (
                raw_outcome.value
                if isinstance(raw_outcome, HealthOutcome)
                else str(raw_outcome)
            )
            if raw_value in FORBIDDEN_OUTCOMES:
                self.store.record_safety_audit(
                    SafetyCategory.HEALTH,
                    account_id=account_id,
                    child_id=child_id,
                    operation_id=operation_id or "",
                    error_code="HEALTH_OUTCOME_FORBIDDEN",
                    metadata={
                        "outcome": raw_value,
                        "rule_version": self.rule.rule_version,
                    },
                )
                raise NurseryError(
                    "HEALTH_OUTCOME_FORBIDDEN",
                    "health rule returned an outcome forbidden by the product boundary",
                )
            try:
                outcome = HealthOutcome(raw_value)
            except ValueError as exc:
                raise NurseryError(
                    "HEALTH_OUTCOME_INVALID", "health rule returned an unknown outcome"
                ) from exc
            return self.store.record_health_evaluation(
                {
                    "evaluation_key": key,
                    "account_id": account_id,
                    "child_id": child_id,
                    "operation_id": operation_id,
                    "rule_version": str(self.rule.rule_version),
                    "trigger_type": event_trigger.value,
                    "condition_hash": condition_hash,
                    "source_type": source.source_type.value,
                    "source_id": source.source_id,
                    "source_version": source.source_version,
                    "child_state_version": int(state["state_version"]),
                    "outcome": outcome.value,
                }
            )
