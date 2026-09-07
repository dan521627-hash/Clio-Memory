"""Validated, deterministic effects for completed everyday care.

The response model may recognise what happened in natural language, but it
never chooses a numeric body or health change.  This small rule layer keeps
that boundary in one place for both validation and the state write.
"""

from __future__ import annotations

from typing import Any

from .models import NurseryError


CARE_KINDS = frozenset(
    {"food", "drink", "rest", "comfort", "play", "warmth", "gift", "world_item", "health", "none"}
)
CARE_STATUSES = frozenset({"completed", "proposed", "none"})
DEFAULT_CARE_OBJECTS = {
    "food": "食物",
    "drink": "水",
    "rest": "休息",
    "comfort": "拥抱和陪伴",
    "play": "一起玩",
    "warmth": "保暖",
    "gift": "礼物",
    "world_item": "房间物品",
    "health": "健康照料",
}


def normalize_care_action(value: Any) -> dict[str, str]:
    """Accept only a compact, structured statement of what really happened."""

    if value is None or value == "":
        return {"kind": "none", "status": "none", "object_name": "", "summary": ""}
    if not isinstance(value, dict) or set(value) - {"kind", "status", "object_name", "summary"}:
        raise NurseryError("INVALID_CHILD_MODEL_OUTPUT", "child care action is invalid")
    kind = str(value.get("kind") or "none").strip().lower()
    status = str(value.get("status") or "none").strip().lower()
    object_name = " ".join(str(value.get("object_name") or "").split())
    summary = " ".join(str(value.get("summary") or "").split())
    if kind not in CARE_KINDS or status not in CARE_STATUSES:
        raise NurseryError("INVALID_CHILD_MODEL_OUTPUT", "child care action is invalid")
    if kind == "none":
        if status != "none" or object_name or summary:
            raise NurseryError("INVALID_CHILD_MODEL_OUTPUT", "empty child care action is invalid")
        return {"kind": kind, "status": status, "object_name": "", "summary": ""}
    if status == "none" or len(object_name) > 120 or len(summary) > 180:
        raise NurseryError("INVALID_CHILD_MODEL_OUTPUT", "child care action is invalid")
    if status == "completed" and not summary:
        raise NurseryError("INVALID_CHILD_MODEL_OUTPUT", "completed child care needs a short fact")
    if status == "completed" and not object_name:
        object_name = DEFAULT_CARE_OBJECTS[kind]
    return {"kind": kind, "status": status, "object_name": object_name, "summary": summary}


def apply_completed_care(
    runtime_state: dict[str, Any], action: dict[str, str], *, stamp: str
) -> tuple[dict[str, Any], dict[str, str]]:
    """Apply bounded everyday-care effects, never a medical conclusion."""

    state = dict(runtime_state)
    if action.get("status") != "completed":
        return state, {}
    kind = str(action.get("kind") or "none")
    clock_updates: dict[str, str] = {"last_care_at": stamp}

    def lower(name: str, amount: float) -> None:
        state[name] = round(max(0.0, float(state.get(name, 0.0)) - amount), 3)

    def raise_(name: str, amount: float) -> None:
        state[name] = round(min(1.0, float(state.get(name, 0.0)) + amount), 3)

    if kind == "food":
        lower("hunger", 0.28)
        clock_updates["last_fed_at"] = stamp
    elif kind == "drink":
        lower("thirst", 0.30)
        clock_updates["last_hydrated_at"] = stamp
    elif kind == "rest":
        lower("fatigue", 0.22)
        raise_("comfort", 0.06)
    elif kind == "comfort":
        raise_("comfort", 0.12)
        raise_("connection", 0.08)
    elif kind == "play":
        lower("play_drive", 0.18)
        raise_("comfort", 0.06)
        raise_("connection", 0.06)
    elif kind == "warmth":
        raise_("comfort", 0.12)
    elif kind == "gift":
        raise_("comfort", 0.04)
        raise_("connection", 0.04)
    # world_item records a world fact; health records an observed/reported
    # fact only.  Neither is allowed to mutate a health value here.
    return state, clock_updates
