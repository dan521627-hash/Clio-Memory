"""Explicit status-response whitelists for each nursery state."""

from __future__ import annotations

from typing import Any

from .models import ModuleState


COMMON_FIELDS = ("module", "module_state", "state_version")
STATUS_FIELDS: dict[ModuleState, tuple[str, ...]] = {
    ModuleState.NEVER_ENABLED: COMMON_FIELDS,
    ModuleState.DRAFT: COMMON_FIELDS + ("child_id", "stage_id"),
    ModuleState.ACTIVE: COMMON_FIELDS
    + (
        "child_id", "stage_id", "active_pause_count", "child_state",
        "child_name", "sex_status",
    ),
    ModuleState.PAUSED: COMMON_FIELDS
    + (
        "child_id", "stage_id", "active_pause_count", "child_state",
        "child_name", "sex_status",
    ),
    ModuleState.DELETION_PENDING: COMMON_FIELDS
    + ("child_id", "recycle_deadline", "restore_state"),
}


def public_status_view(row: dict[str, Any]) -> dict[str, Any]:
    state = ModuleState(row["module_state"])
    return {key: row.get(key) for key in STATUS_FIELDS[state]}
