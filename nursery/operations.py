"""Canonical operation identifiers and request hashing."""

from __future__ import annotations

import hashlib
import json
import uuid
from typing import Any

from .models import NurseryCommand, NurseryError


def validate_operation_id(value: str) -> str:
    raw = str(value).strip()
    try:
        parsed = uuid.UUID(raw)
    except (ValueError, AttributeError, TypeError) as exc:
        raise NurseryError(
            "INVALID_OPERATION_ID", "operation_id must be a UUID"
        ) from exc
    return str(parsed)


def validate_idempotency_key(value: str) -> str:
    key = str(value).strip()
    if not key or len(key) > 200:
        raise NurseryError(
            "INVALID_IDEMPOTENCY_KEY",
            "idempotency_key must contain 1 to 200 characters",
        )
    return key


def canonical_json(value: Any) -> str:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise NurseryError(
            "INVALID_REQUEST_PAYLOAD", "request payload must be canonical JSON"
        ) from exc


def request_hash(command: NurseryCommand) -> str:
    private_payload_hash = None
    if command.private_payload is not None:
        private_payload_hash = hashlib.sha256(
            canonical_json(command.private_payload).encode("utf-8")
        ).hexdigest()
    material = {
        "account_id": command.actor.account_id,
        "caregiver_id": command.actor.caregiver_id,
        "child_id": command.child_id,
        "action": command.action.value,
        "expected_state_version": command.expected_state_version,
        "payload": command.payload,
        "private_payload_hash": private_payload_hash,
        "source": (
            {
                "type": command.source.source_type.value,
                "id": command.source.source_id,
                "version": command.source.source_version,
                "specification_ref": command.source.specification_ref,
            }
            if command.source
            else None
        ),
    }
    return hashlib.sha256(canonical_json(material).encode("utf-8")).hexdigest()
