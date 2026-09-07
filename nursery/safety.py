"""Permission gates and minimal, non-sensitive safety metadata."""

from __future__ import annotations

from typing import Any

from .models import ActorContext, NurseryAction, NurseryError, ensure_actor_allowed


SAFE_AUDIT_METADATA_KEYS = frozenset(
    {"action", "state", "source_type", "outcome", "reason", "rule_version"}
)

FORBIDDEN_PUBLIC_PAYLOAD_KEYS = frozenset(
    {
        "answers",
        "api_key",
        "apikey",
        "credential",
        "password",
        "private_payload",
        "raw_prompt",
        "secret",
        "token",
    }
)


def validate_actor(actor: ActorContext, action: NurseryAction) -> None:
    if not isinstance(actor, ActorContext):
        raise NurseryError(
            "UNTRUSTED_ACTOR_CONTEXT",
            "actor context must be constructed by a trusted adapter",
        )
    ensure_actor_allowed(actor, action)


def validate_public_operation_payload(value: Any) -> None:
    """Fail closed before operation JSON can persist credentials or raw answers."""

    def walk(candidate: Any) -> None:
        if isinstance(candidate, dict):
            for key, nested in candidate.items():
                normalized = str(key).strip().lower().replace("-", "_")
                if normalized in FORBIDDEN_PUBLIC_PAYLOAD_KEYS:
                    raise NurseryError(
                        "PRIVATE_DATA_IN_PUBLIC_PAYLOAD",
                        "private values must use the protected ephemeral payload",
                    )
                walk(nested)
        elif isinstance(candidate, list):
            for nested in candidate:
                walk(nested)

    if not isinstance(value, dict):
        raise NurseryError("INVALID_REQUEST_PAYLOAD", "operation payload must be an object")
    walk(value)


def sanitize_audit_metadata(metadata: dict[str, Any] | None) -> dict[str, Any]:
    """Never persist arbitrary prompts, questionnaire text, or private reasons."""

    clean: dict[str, Any] = {}
    for key, value in (metadata or {}).items():
        if key not in SAFE_AUDIT_METADATA_KEYS:
            continue
        if isinstance(value, (str, int, float, bool)) or value is None:
            clean[key] = value
    return clean
