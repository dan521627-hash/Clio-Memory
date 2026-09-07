"""Deterministic nursery body-time projection with no background work."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping


BODY_FIELDS = ("hunger", "thirst", "fatigue")


def nursery_body_time_rule() -> "BodyTimeRule":
    """Return the deliberately modest, versioned care-state clock for production.

    These are *simulation state* rates, not medical measurements.  They only
    make an already-created child's care state move between real, recorded
    interactions; illness remains owned by the health/care workflow.
    """

    return BodyTimeRule(
        version="nursery-care-state-v2",
        per_hour={"hunger": 0.035, "thirst": 0.045, "fatigue": 0.025},
        maximum={"hunger": 0.8, "thirst": 0.8, "fatigue": 0.8},
        notice_at={"hunger": 0.62, "thirst": 0.58, "fatigue": 0.68},
    )


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


@dataclass(frozen=True)
class BodyTimeRule:
    """Versioned rule injected by configuration; no product values are guessed."""

    version: str = "body-time-disabled-v1"
    per_hour: Mapping[str, float] | None = None
    maximum: Mapping[str, float] | None = None
    notice_at: Mapping[str, float] | None = None

    @property
    def enabled(self) -> bool:
        return bool(self.per_hour)

    def validate(self) -> None:
        if not str(self.version).strip():
            raise ValueError("body time rule version is required")
        if self.per_hour is None and self.maximum is None:
            return
        if not isinstance(self.per_hour, Mapping) or not isinstance(self.maximum, Mapping):
            raise ValueError("body time per_hour and maximum must be mappings")
        for name in BODY_FIELDS:
            rate = self.per_hour.get(name)
            limit = self.maximum.get(name)
            if type(rate) not in {int, float} or type(limit) not in {int, float}:
                raise ValueError(f"body time config is missing numeric {name}")
            if not -1 <= float(rate) <= 1 or not 0 <= float(limit) <= 1:
                raise ValueError(f"body time config is outside the safe range for {name}")
        if self.notice_at is not None:
            if not isinstance(self.notice_at, Mapping):
                raise ValueError("body time notice_at must be a mapping")
            for name in BODY_FIELDS:
                threshold = self.notice_at.get(name)
                if type(threshold) not in {int, float}:
                    raise ValueError(f"body time config is missing numeric notice_at.{name}")
                if not 0 <= float(threshold) <= 1:
                    raise ValueError(
                        f"body time notice threshold is outside the safe range for {name}"
                    )

    def project(
        self,
        saved: Mapping[str, Any],
        *,
        settled_through: datetime,
        evaluated_at: datetime,
        sleeping: bool = False,
    ) -> dict[str, float]:
        self.validate()
        result = {name: float(saved.get(name, 0.0)) for name in BODY_FIELDS}
        if not self.enabled:
            return result
        elapsed_hours = max(
            0.0, (_utc(evaluated_at) - _utc(settled_through)).total_seconds() / 3600
        )
        for name in BODY_FIELDS:
            rate = float((self.per_hour or {}).get(name, 0.0))
            if sleeping and name == "fatigue":
                rate = -abs(rate)
            limit = float((self.maximum or {}).get(name, 1.0))
            # A safety ceiling must not erase a previously observed higher need.
            if rate >= 0:
                limit = max(limit, result[name])
            result[name] = round(min(limit, max(0.0, result[name] + rate * elapsed_hours)), 3)
        return result


def project_life_state(saved, clock, *, evaluated_at, rule, active):
    """One deterministic read/write projection; no model and no background loop."""
    runtime, projected_clock = dict(saved), dict(clock)
    settled = clock.get("settled_through")
    settled = datetime.fromisoformat(settled.replace("Z", "+00:00")) if isinstance(settled, str) else settled
    settled = settled if isinstance(settled, datetime) else evaluated_at
    if not active or not rule.enabled or _utc(evaluated_at) <= _utc(settled):
        return runtime, projected_clock
    sleep_start = clock.get("sleep_started_at")
    if sleep_start and rule.version == "nursery-care-state-v2":
        start = datetime.fromisoformat(sleep_start.replace("Z", "+00:00"))
        # Simulation rest window, not a medical sleep recommendation.
        wake = _utc(start) + timedelta(hours=8)
        sleep_end = min(_utc(evaluated_at), max(_utc(settled), wake))
        needs = rule.project(runtime, settled_through=settled, evaluated_at=sleep_end, sleeping=True)
        if _utc(evaluated_at) > sleep_end:
            needs = rule.project(needs, settled_through=sleep_end, evaluated_at=evaluated_at)
        runtime.update(needs)
        if _utc(evaluated_at) >= wake:
            projected_clock.update(sleep_started_at=None, last_woke_at=wake.isoformat(timespec="microseconds"))
    else:
        runtime.update(rule.project(runtime, settled_through=settled, evaluated_at=evaluated_at, sleeping=bool(sleep_start)))
    if rule.version == "nursery-care-state-v2" and isinstance(saved.get("emotion"), dict):
        emotion = dict(saved["emotion"])
        elapsed = (_utc(evaluated_at)-_utc(settled)).total_seconds()/3600
        if type(emotion.get("arousal")) in {int, float}:
            emotion["arousal"] = .25 + (emotion["arousal"]-.25) * (0.5 ** (elapsed/4))
        # Keep the reason/primary emotion and unresolved discomfort; do not invent a happy event.
        runtime["emotion"] = emotion
    return runtime, projected_clock


def effective_body_view(
    saved: Mapping[str, Any],
    clock: Mapping[str, Any],
    *,
    evaluated_at: datetime,
    rule: BodyTimeRule,
    active: bool,
) -> dict[str, Any]:
    """Pure read projection. It never consumes the settlement cursor."""

    settled = clock.get("settled_through")
    if isinstance(settled, str):
        settled = datetime.fromisoformat(settled.replace("Z", "+00:00"))
    if not isinstance(settled, datetime):
        settled = evaluated_at
    runtime, projected_clock = project_life_state(saved, clock, evaluated_at=evaluated_at, rule=rule, active=active)
    needs = {name: float(runtime.get(name, 0.0)) for name in BODY_FIELDS}
    thresholds = rule.notice_at or {}
    labels = [
        name
        for name, value in needs.items()
        if name in thresholds and value >= float(thresholds[name])
    ]
    return {
        "effective_needs": needs,
        "effective_runtime": runtime,
        "body_needs": labels,
        "time_context": {
            "last_fed_at": clock.get("last_fed_at"),
            "last_hydrated_at": clock.get("last_hydrated_at"),
            "sleep_started_at": projected_clock.get("sleep_started_at"),
            "last_woke_at": projected_clock.get("last_woke_at"),
            "last_care_at": clock.get("last_care_at"),
            "evaluated_at": _utc(evaluated_at).isoformat(timespec="microseconds"),
            "next_change_hint": None,
            "settlement_due": bool(active and rule.enabled and _utc(evaluated_at) > _utc(settled)),
            "rule_version": rule.version,
        },
    }


def rule_from_config(config: Mapping[str, Any] | None) -> BodyTimeRule:
    settings = dict(config or {})
    rates = settings.get("per_hour")
    maximum = settings.get("maximum")
    notice_at = settings.get("notice_at")
    if rates is None:
        return BodyTimeRule(version=str(settings.get("rule_version") or "body-time-disabled-v1"))
    if not isinstance(rates, Mapping) or not isinstance(maximum, Mapping):
        raise ValueError("body time per_hour and maximum must be mappings")
    rule = BodyTimeRule(
        version=str(settings.get("rule_version") or "").strip() or "body-time-configured-v1",
        per_hour={name: rates.get(name) for name in BODY_FIELDS},
        maximum={name: maximum.get(name) for name in BODY_FIELDS},
        notice_at=(
            {name: notice_at.get(name) for name in BODY_FIELDS}
            if isinstance(notice_at, Mapping)
            else notice_at
        ),
    )
    rule.validate()
    return BodyTimeRule(
        version=rule.version,
        per_hour={name: float(rule.per_hour[name]) for name in BODY_FIELDS},
        maximum={name: float(rule.maximum[name]) for name in BODY_FIELDS},
        notice_at=(
            {name: float(rule.notice_at[name]) for name in BODY_FIELDS}
            if rule.notice_at is not None
            else None
        ),
    )
