"""Stage-2 creation coordinator layered on the shared nursery operation pipeline."""

from __future__ import annotations

import asyncio
import hashlib
import uuid
from pathlib import Path
from typing import Any

from .body_state import BodyTimeRule
from .capability_rules import CapabilityRules, load_capability_rules, validate_growth_observation
from .coordinator import NurseryCoordinator, PreparedMutation
from .creation_rules import (
    INITIAL_STYLE_QUESTIONNAIRE_VERSION,
    TEMPERAMENT_FORMULA_VERSION,
    TEMPERAMENT_QUESTIONNAIRE_VERSION,
    independent_temperament,
    initial_style_profile,
    temperament_tendency,
)
from .model_connection import (
    AdapterRegistry,
    ModelConnectionRequest,
    default_adapter_registry,
    validate_connection_request,
)
from .models import (
    ActorContext,
    CaregiverRole,
    NurseryAction,
    NurseryCommand,
    NurseryError,
    OperationResult,
    OperationStatus,
    QuestionnaireKind,
    SourceReference,
)
from .operations import canonical_json
from .queue import ChildLockRegistry, QueueConfig
from .secret_vault import SecretVault
from .store import NurseryStore, iso_time


class NurseryCreationService:
    """The only stage-2 creation facade; pages and tools will call this later."""

    def __init__(
        self,
        store: NurseryStore,
        *,
        vault: SecretVault,
        capability_rules: CapabilityRules | str | Path,
        adapter_registry: AdapterRegistry | None = None,
        queue_config: QueueConfig | None = None,
        locks: ChildLockRegistry | None = None,
        body_time_rule: BodyTimeRule | None = None,
    ) -> None:
        self.store = store
        self.vault = vault
        self.capability_rules = (
            load_capability_rules(capability_rules)
            if isinstance(capability_rules, (str, Path))
            else capability_rules
        )
        self.adapter_registry = adapter_registry or default_adapter_registry()
        self.coordinator = NurseryCoordinator(
            store,
            queue_config=queue_config,
            locks=locks,
            prepare_hook=self._prepare,
            body_time_rule=body_time_rule,
        )

    @staticmethod
    def child_id_for(account_id: str, operation_id: str) -> str:
        return str(
            uuid.uuid5(
                uuid.NAMESPACE_URL,
                f"anima:nursery:{str(account_id).strip()}:{str(operation_id).strip()}",
            )
        )

    async def start_draft(
        self,
        *,
        actor: ActorContext,
        operation_id: str,
        idempotency_key: str,
        source: SourceReference,
        display_name: str,
    ) -> OperationResult:
        if actor.role != CaregiverRole.USER_GUARDIAN:
            raise NurseryError(
                "USER_GUARDIAN_REQUIRED", "only the user can start a creation draft"
            )
        child_id = self.child_id_for(actor.account_id, operation_id)
        command = NurseryCommand(
            actor=actor,
            child_id=child_id,
            action=NurseryAction.START_DRAFT,
            operation_id=operation_id,
            idempotency_key=idempotency_key,
            expected_state_version=0,
            payload={"display_name": str(display_name).strip()},
            source=source,
        )
        existing = await asyncio.to_thread(self.store.get_operation, operation_id)
        operation = await asyncio.to_thread(
            self.store.bootstrap_creation_draft,
            command,
            child_id=child_id,
            display_name=display_name,
            independent_temperament=independent_temperament(child_id),
            temperament_formula_version=TEMPERAMENT_FORMULA_VERSION,
        )
        return NurseryCoordinator._from_operation(
            operation, replayed=existing is not None
        )

    async def submit(
        self,
        command: NurseryCommand,
        *,
        resume_retryable: bool = False,
    ) -> OperationResult:
        if command.action == NurseryAction.START_DRAFT:
            raise NurseryError(
                "USE_CREATION_BOOTSTRAP",
                "start_draft must use the atomic creation bootstrap",
            )
        if command.action == NurseryAction.SAVE_DRAFT_IDENTITY:
            stage_id = str(command.payload.get("stage_id") or "")
            if stage_id not in {
                str(stage["id"]) for stage in self.capability_rules.stages
            }:
                raise NurseryError(
                    "INVALID_STAGE", "stage must come from the capability YAML"
                )
        return await self.coordinator.submit(
            command, resume_retryable=resume_retryable
        )

    async def _prepare(
        self,
        command: NurseryCommand,
        before: dict[str, Any],
    ) -> PreparedMutation | None:
        if command.action == NurseryAction.RECORD_GROWTH_OBSERVATION:
            validate_growth_observation(command.payload, self.capability_rules, str(before["stage_id"]))
        if command.action in {
            NurseryAction.SAVE_MODEL_CONNECTION,
            NurseryAction.RETEST_MODEL_CONNECTION,
        }:
            return await self._prepare_model_connection(command)
        if command.action == NurseryAction.DELETE_MODEL_CONNECTION:
            old_config = await asyncio.to_thread(
                self.store.get_model_config_internal, command.actor.account_id
            )
            if not old_config:
                raise NurseryError(
                    "MODEL_NOT_CONFIGURED", "child model connection does not exist"
                )
            old_reference = str(old_config["credential_ref"])

            async def delete_after_commit(_: dict[str, Any]) -> None:
                await asyncio.to_thread(self.vault.delete, old_reference)

            return PreparedMutation(
                data={"credential_ref": old_reference},
                on_commit=delete_after_commit,
            )
        if command.action in {
            NurseryAction.SUBMIT_TEMPERAMENT_QUESTIONNAIRE,
            NurseryAction.SUBMIT_INITIAL_STYLE,
        }:
            return await self._prepare_questionnaire(command)
        if command.action == NurseryAction.CONFIRM_CREATION:
            refs = await asyncio.to_thread(
                self.store.private_refs_for_child, command.child_id
            )

            async def purge_if_activated(operation: dict[str, Any]) -> None:
                result = operation.get("result") or {}
                if result.get("module_state") == "active":
                    for reference in refs:
                        if reference != "purged":
                            await asyncio.to_thread(self.vault.delete, reference)

            return PreparedMutation(on_commit=purge_if_activated)
        return None

    async def _prepare_model_connection(
        self, command: NurseryCommand
    ) -> PreparedMutation:
        old_config = await asyncio.to_thread(
            self.store.get_model_config_internal, command.actor.account_id
        )
        if command.action == NurseryAction.RETEST_MODEL_CONNECTION:
            if not old_config:
                raise NurseryError(
                    "MODEL_NOT_CONFIGURED", "child model connection does not exist"
                )
            credential = await asyncio.to_thread(
                self.vault.get, str(old_config["credential_ref"])
            )
            request = validate_connection_request(
                ModelConnectionRequest(
                    provider_id=str(old_config["provider_id"]),
                    base_url=str(old_config["base_url"]),
                    model_name=str(old_config["model_name"]),
                    credential=credential,
                    timeout_seconds=float(command.payload.get("timeout_seconds") or 20),
                )
            )
        else:
            private = command.private_payload or {}
            request = validate_connection_request(
                ModelConnectionRequest(
                    provider_id=str(command.payload.get("provider_id") or ""),
                    base_url=str(command.payload.get("base_url") or ""),
                    model_name=str(command.payload.get("model_name") or ""),
                    credential=str(private.get("credential") or ""),
                    timeout_seconds=float(command.payload.get("timeout_seconds") or 20),
                )
            )
        adapter = self.adapter_registry.get(request.provider_id)
        capabilities = await asyncio.to_thread(adapter.test_connection, request)
        if not capabilities.ready:
            raise RuntimeError("child model capability check did not pass")
        reference = (
            str(old_config["credential_ref"])
            if command.action == NurseryAction.RETEST_MODEL_CONNECTION
            else f"model:{command.actor.account_id}:{command.operation_id}"
        )
        old_reference = str(old_config["credential_ref"]) if old_config else ""
        if command.action == NurseryAction.SAVE_MODEL_CONNECTION:
            await asyncio.to_thread(self.vault.put, reference, request.credential)

        async def commit(_: dict[str, Any]) -> None:
            if old_reference and old_reference != reference:
                await asyncio.to_thread(self.vault.delete, old_reference)

        async def abort(_: dict[str, Any]) -> None:
            if command.action == NurseryAction.SAVE_MODEL_CONNECTION:
                await asyncio.to_thread(self.vault.delete, reference)

        return PreparedMutation(
            data={
                "provider_id": request.provider_id,
                "base_url": request.base_url,
                "model_name": request.model_name,
                "credential_ref": reference,
                "credential_suffix": request.credential[-4:],
                "capabilities": capabilities.as_dict(),
                "tested_at": iso_time(self.store.clock()),
                "retest": command.action == NurseryAction.RETEST_MODEL_CONNECTION,
            },
            on_commit=commit,
            on_abort=abort,
        )

    async def _prepare_questionnaire(
        self, command: NurseryCommand
    ) -> PreparedMutation:
        private = command.private_payload or {}
        answers = private.get("answers")
        if command.action == NurseryAction.SUBMIT_TEMPERAMENT_QUESTIONNAIRE:
            kind = QuestionnaireKind.TEMPERAMENT.value
            version = TEMPERAMENT_QUESTIONNAIRE_VERSION
            normalized = temperament_tendency(answers)
        else:
            kind = QuestionnaireKind.INITIAL_STYLE.value
            version = INITIAL_STYLE_QUESTIONNAIRE_VERSION
            normalized = initial_style_profile(answers)
        encoded = canonical_json(answers)
        digest = hashlib.sha256(encoded.encode("utf-8")).hexdigest()
        reference = (
            f"questionnaire:{command.child_id}:{command.actor.caregiver_id}:"
            f"{kind}:{command.operation_id}"
        )
        old_reference = await asyncio.to_thread(
            self.store.get_private_submission_ref,
            child_id=command.child_id,
            caregiver_id=command.actor.caregiver_id,
            questionnaire_kind=kind,
        )
        await asyncio.to_thread(self.vault.put, reference, encoded)

        async def commit(_: dict[str, Any]) -> None:
            if old_reference and old_reference not in {reference, "purged"}:
                await asyncio.to_thread(self.vault.delete, old_reference)

        async def abort(_: dict[str, Any]) -> None:
            await asyncio.to_thread(self.vault.delete, reference)

        return PreparedMutation(
            data={
                "questionnaire_kind": kind,
                "questionnaire_version": version,
                "answer_digest": digest,
                "private_ref": reference,
                "normalized": normalized,
            },
            on_commit=commit,
            on_abort=abort,
        )
