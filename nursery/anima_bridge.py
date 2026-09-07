"""The narrow, one-way handoff from Anima memory management to nursery."""

from __future__ import annotations

import hashlib
import asyncio
import uuid
from typing import Any

from .anima_context import build_child_safe_family_event, build_child_safe_memory_event
from .coordinator import NurseryCoordinator, PreparedMutation
from .health_runtime import NurseryHealthRuntime
from .model_connection import (
    AdapterRegistry,
    ChildFamilyEventRequest,
    ModelConnectionRequest,
    default_adapter_registry,
    validate_connection_request,
)
from .models import (
    ActorContext,
    AuthSource,
    CaregiverRole,
    NurseryAction,
    NurseryCommand,
    NurseryError,
    SourceReference,
    SourceType,
)
from .store import NurseryStore
from .secret_vault import SecretVault


class AnimaNurseryBridge:
    """Store only an already child-safe memory-AI summary for a child turn."""

    def __init__(
        self,
        store: NurseryStore,
        *,
        vault: SecretVault | None = None,
        adapter_registry: AdapterRegistry | None = None,
        health_runtime: NurseryHealthRuntime | None = None,
    ) -> None:
        self.store = store
        self.vault = vault
        self.adapter_registry = adapter_registry or default_adapter_registry()
        self.health_runtime = health_runtime or NurseryHealthRuntime(store)
        self.coordinator = NurseryCoordinator(store, prepare_hook=self._prepare)

    async def _prepare(
        self, command: NurseryCommand, before: dict[str, Any]
    ) -> PreparedMutation:
        context = (command.private_payload or {}).get("anima_context")
        if not isinstance(context, dict):
            raise NurseryError(
                "ANIMA_CONTEXT_PREPARATION_REQUIRED",
                "a validated Anima child context is required",
            )
        if command.action == NurseryAction.SYNC_ANIMA_CONTEXT:
            return PreparedMutation(data={"anima_context": dict(context)})
        if command.action != NurseryAction.APPLY_ANIMA_FAMILY_EVENT:
            raise NurseryError("INVALID_ANIMA_ACTION", "Anima bridge action is invalid")
        if self.vault is None:
            raise NurseryError("MODEL_NOT_CONFIGURED", "a ready child response model is required")
        config = await asyncio.to_thread(
            self.store.get_model_config_internal, command.actor.account_id
        )
        if not config or config.get("connection_status") != "ready":
            raise NurseryError("MODEL_NOT_CONFIGURED", "a ready child response model is required")
        credential = await asyncio.to_thread(
            self.vault.get, str(config["credential_ref"])
        )
        request = validate_connection_request(
            ModelConnectionRequest(
                provider_id=str(config["provider_id"]),
                base_url=str(config["base_url"]),
                model_name=str(config["model_name"]),
                credential=credential,
            )
        )
        runtime_state = await asyncio.to_thread(
            self.store.get_child_runtime_state, command.child_id
        )
        proposal = await asyncio.to_thread(
            self.adapter_registry.get(request.provider_id).respond_to_family_event,
            request,
            ChildFamilyEventRequest(
                category=str(context["category"]),
                summary=str(context["summary"]),
                stage_id=str(before["stage_id"]),
                runtime_state=runtime_state,
                anima_context=dict(context),
            ),
        )
        reaction = " ".join(str(proposal.child_reaction or "").split())
        if not reaction or len(reaction) > 240:
            raise NurseryError("INVALID_CHILD_MODEL_OUTPUT", "child reaction is invalid")
        ripple = proposal.current_ripple
        if ripple is not None:
            ripple = " ".join(str(ripple).split())
            if not ripple or len(ripple) > 240:
                raise NurseryError("INVALID_CHILD_MODEL_OUTPUT", "child ripple is invalid")

        async def evaluate_health_after_commit(_operation: dict[str, Any]) -> None:
            await asyncio.to_thread(
                self.health_runtime.evaluate_family_event,
                account_id=command.actor.account_id,
                child_id=command.child_id,
                source=command.source,
                operation_id=command.operation_id,
                health_relevant=context.get("health_relevant") is True,
            )

        return PreparedMutation(
            data={
                "experience_id": "family:" + command.operation_id,
                "context": dict(context),
                "occurred_at": str(context["occurred_at"]),
                "category": "family",
                "summary": str(context["summary"]),
                "child_reaction": reaction,
                "current_ripple": ripple,
                "requires_attention": False,
            },
            on_commit=evaluate_health_after_commit,
        )

    async def accept(
        self,
        *,
        child_id: str,
        child_safe_event: dict[str, Any],
        disposition_report: dict[str, Any],
    ) -> dict[str, Any]:
        try:
            context = build_child_safe_memory_event(
                child_safe_event, disposition_report=disposition_report
            )
        except (TypeError, ValueError) as exc:
            raise NurseryError(
                "INVALID_ANIMA_CHILD_SUMMARY",
                "Anima must provide an explicit child-safe memory summary",
            ) from exc
        state = self.store.get_state_for_child(child_id)
        source_key = str(context["source_key"])
        source_version = str(context["source_version"])
        namespace = f"anima:nursery:{state['account_id']}:{child_id}:{source_key}:{source_version}"
        operation_id = str(uuid.uuid5(uuid.NAMESPACE_URL, namespace))
        idempotency_key = hashlib.sha256(namespace.encode("utf-8")).hexdigest()[:48]
        actor = ActorContext.build(
            account_id=str(state["account_id"]),
            caregiver_id=f"anima-memory-manager:{state['account_id']}",
            role=CaregiverRole.SYSTEM_EVENT,
            permissions={"nursery.anima.context.sync"},
            auth_source=AuthSource.INTERNAL_EVENT,
        )
        result = await self.coordinator.submit(
            NurseryCommand(
                actor=actor,
                child_id=str(child_id),
                action=NurseryAction.SYNC_ANIMA_CONTEXT,
                operation_id=operation_id,
                idempotency_key=idempotency_key,
                expected_state_version=None,
                payload={},
                private_payload={"anima_context": context},
                source=SourceReference.build(
                    source_type=SourceType.ANIMA_MEMORY,
                    source_id=source_key,
                    source_version=source_version,
                    specification_ref="anima-nursery-safe-context-v1",
                ),
            )
        )
        if result.status.value != "completed":
            raise NurseryError(
                result.error_code or "ANIMA_CONTEXT_SYNC_FAILED",
                result.error_message or "Anima context was not accepted by nursery",
            )
        return dict(result.result)

    async def apply_family_event(
        self,
        *,
        child_id: str,
        child_safe_event: dict[str, Any],
        disposition_report: dict[str, Any],
        source_type: SourceType = SourceType.MAILBOX_EVENT,
    ) -> dict[str, Any]:
        """Wake an active child exactly once for a vetted real family event."""

        source_type = SourceType(source_type)
        try:
            context = build_child_safe_family_event(
                child_safe_event, disposition_report=disposition_report
            )
        except (TypeError, ValueError) as exc:
            raise NurseryError(
                "INVALID_ANIMA_CHILD_SUMMARY",
                "Anima must provide an explicit child-safe family summary",
            ) from exc
        state = self.store.get_state_for_child(child_id)
        if state["module_state"] != "active":
            return {"family_event_ignored": "nursery_not_active"}
        if source_type not in {SourceType.MAILBOX_EVENT, SourceType.ANIMA_MEMORY}:
            raise NurseryError(
                "INVALID_ANIMA_FAMILY_SOURCE",
                "family events require a vetted mailbox or memory source",
            )
        if context["health_relevant"] and source_type != SourceType.MAILBOX_EVENT:
            raise NurseryError(
                "HEALTH_MAILBOX_SOURCE_REQUIRED",
                "health-relevant family events require a mailbox source",
            )
        source_key = str(context["source_key"])
        source_version = str(context["source_version"])
        namespace = (
            f"anima:nursery:family:{state['account_id']}:{child_id}:"
            f"{source_type.value}:{source_key}:{source_version}"
        )
        operation_id = str(uuid.uuid5(uuid.NAMESPACE_URL, namespace))
        idempotency_key = hashlib.sha256(namespace.encode("utf-8")).hexdigest()[:48]
        actor = ActorContext.build(
            account_id=str(state["account_id"]),
            caregiver_id=f"anima-memory-manager:{state['account_id']}",
            role=CaregiverRole.SYSTEM_EVENT,
            permissions={"nursery.anima.family_event.apply"},
            auth_source=AuthSource.INTERNAL_EVENT,
        )
        result = await self.coordinator.submit(
            NurseryCommand(
                actor=actor,
                child_id=str(child_id),
                action=NurseryAction.APPLY_ANIMA_FAMILY_EVENT,
                operation_id=operation_id,
                idempotency_key=idempotency_key,
                expected_state_version=None,
                payload={},
                private_payload={"anima_context": context},
                source=SourceReference.build(
                    source_type=source_type,
                    source_id=source_key,
                    source_version=source_version,
                    specification_ref="anima-nursery-family-event-v1",
                ),
            )
        )
        if result.status.value != "completed":
            raise NurseryError(
                result.error_code or "ANIMA_FAMILY_EVENT_FAILED",
                result.error_message or "Anima family event was not accepted by nursery",
            )
        return dict(result.result)
