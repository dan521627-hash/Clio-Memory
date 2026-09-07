from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import tempfile
import time
import unittest
import uuid
from contextlib import closing
from pathlib import Path

from nursery.capability_rules import load_capability_rules
from nursery.creation_queries import NurseryCreationQueryService
from nursery.creation_rules import (
    INITIAL_STYLE_QUESTIONS,
    TEMPERAMENT_QUESTIONS,
    independent_temperament,
    name_candidate_id,
)
from nursery.creation_service import NurseryCreationService
from nursery.external_tools import ExternalNurseryTools
from nursery.model_connection import (
    AdapterRegistry,
    ModelCapabilities,
    ModelConnectionRequest,
    OpenAICompatibleChildModelAdapter,
)
from nursery.models import (
    ActorContext,
    AuthSource,
    CaregiverRole,
    ModuleState,
    NurseryAction,
    NurseryCommand,
    NurseryError,
    OperationStatus,
    SourceReference,
    SourceType,
)
from nursery.secret_vault import FernetFileSecretVault, InMemorySecretVault
from nursery.store import NurseryStore


RULES_PATH = Path(
    os.environ.get(
        "ANIMA_NURSERY_RULES",
        str(Path(__file__).resolve().parent / "nursery" / "capability-rules-v1.yaml"),
    )
)


def guardian(
    caregiver_id: str,
    role: CaregiverRole,
    *,
    account_id: str = "account",
) -> ActorContext:
    return ActorContext.build(
        account_id=account_id,
        caregiver_id=caregiver_id,
        role=role,
        permissions=["nursery.*"],
        auth_source=(
            AuthSource.MANAGER_SESSION
            if role == CaregiverRole.USER_GUARDIAN
            else AuthSource.MCP_SESSION
        ),
    )


class FakeModelAdapter:
    def __init__(self) -> None:
        self.calls = 0
        self.fail = False
        self.delay_seconds = 0.0
        self.last_request: ModelConnectionRequest | None = None

    def test_connection(self, request: ModelConnectionRequest) -> ModelCapabilities:
        self.calls += 1
        self.last_request = request
        if self.delay_seconds:
            time.sleep(self.delay_seconds)
        if self.fail:
            raise ConnectionError("offline")
        return ModelCapabilities(True, True, True, True)


class CountingVault(InMemorySecretVault):
    def __init__(self) -> None:
        super().__init__()
        self.put_calls = 0

    def put(self, reference: str, value: str) -> None:
        self.put_calls += 1
        super().put(reference, value)


class NurseryCreationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp.name) / "nursery.sqlite3"
        self.store = NurseryStore(self.db_path)
        self.vault = CountingVault()
        self.adapter = FakeModelAdapter()
        registry = AdapterRegistry()
        registry.register("fake", self.adapter)
        self.service = NurseryCreationService(
            self.store,
            vault=self.vault,
            capability_rules=load_capability_rules(RULES_PATH),
            adapter_registry=registry,
        )
        self.queries = NurseryCreationQueryService(self.store)
        self.user = guardian("user-guardian", CaregiverRole.USER_GUARDIAN)
        self.external = guardian(
            "anima-external-ai-guardian", CaregiverRole.EXTERNAL_AI_GUARDIAN
        )
        self.serial = 0
        self.child_id = ""

    async def asyncTearDown(self):
        self.temp.cleanup()

    def source(self, actor: ActorContext | None = None) -> SourceReference:
        self.serial += 1
        selected = actor or self.user
        return SourceReference.build(
            source_type=(
                SourceType.EXTERNAL_AI_INTERACTION
                if selected.role == CaregiverRole.EXTERNAL_AI_GUARDIAN
                else SourceType.USER_INTERACTION
            ),
            source_id=f"stage2-source-{self.serial}",
            source_version="v1",
            specification_ref="nursery-stage2-creation-test",
        )

    def command(
        self,
        action: NurseryAction,
        *,
        actor: ActorContext | None = None,
        payload: dict | None = None,
        private_payload: dict | None = None,
        expected: int | None = None,
        operation_id: str | None = None,
        idempotency_key: str | None = None,
        source: SourceReference | None = None,
    ) -> NurseryCommand:
        selected = actor or self.user
        if expected is None:
            expected = int(
                self.store.get_state(selected.account_id, self.child_id)["state_version"]
            )
        self.serial += 1
        return NurseryCommand(
            actor=selected,
            child_id=self.child_id,
            action=action,
            operation_id=operation_id or str(uuid.uuid4()),
            idempotency_key=idempotency_key or f"stage2-idempotency-{self.serial}",
            expected_state_version=expected,
            payload=dict(payload or {}),
            source=source or self.source(selected),
            private_payload=private_payload,
        )

    async def start(self) -> OperationStatus:
        operation_id = str(uuid.uuid4())
        result = await self.service.start_draft(
            actor=self.user,
            operation_id=operation_id,
            idempotency_key="start-draft",
            source=self.source(self.user),
            display_name="用户",
        )
        self.child_id = str(result.result["child_id"])
        return result.status

    async def bind_external(self):
        return await self.service.submit(
            self.command(
                NurseryAction.BIND_EXTERNAL_GUARDIAN,
                payload={
                    "caregiver_id": self.external.caregiver_id,
                    "display_name": "沈野",
                },
            )
        )

    async def save_model(self, credential: str = "sk-stage2-secret-A"):
        return await self.service.submit(
            self.command(
                NurseryAction.SAVE_MODEL_CONNECTION,
                payload={
                    "provider_id": "fake",
                    "base_url": "https://models.example/v1",
                    "model_name": "child-model",
                    "timeout_seconds": 5,
                },
                private_payload={"credential": credential},
            )
        )

    def proposals(self, actor: ActorContext, names: list[str]) -> list[dict]:
        return [
            {
                "candidate_id": name_candidate_id(
                    self.child_id, actor.caregiver_id, ordinal, name
                ),
                "name": name,
                "meaning": f"{name}的含义",
                "sound_notes": "读起来柔和",
                "avoid_notes": "",
            }
            for ordinal, name in enumerate(names, 1)
        ]

    async def make_ready_for_confirmation(self) -> None:
        await self.bind_external()
        await self.save_model()
        await self.service.submit(
            self.command(
                NurseryAction.SAVE_DRAFT_IDENTITY,
                payload={
                    "sex_status": "boy",
                    "stage_id": "infancy",
                    "nickname": "小满",
                    "address_terms": {
                        "user_guardian": "妈妈",
                        "external_ai_guardian": "沈野",
                    },
                },
            )
        )
        user_proposals = self.proposals(self.user, ["安禾", "小满"])
        external_proposals = self.proposals(self.external, ["安禾", "宁宁"])
        await self.service.submit(
            self.command(
                NurseryAction.SAVE_NAME_PROPOSALS,
                payload={"proposals": user_proposals},
            )
        )
        await self.service.submit(
            self.command(
                NurseryAction.SAVE_NAME_PROPOSALS,
                actor=self.external,
                payload={"proposals": external_proposals},
            )
        )
        all_candidates = user_proposals + external_proposals
        selected = user_proposals[0]["candidate_id"]
        for actor in (self.user, self.external):
            preferences = {
                item["candidate_id"]: (
                    "like" if item["candidate_id"] == selected else "acceptable"
                )
                for item in all_candidates
            }
            await self.service.submit(
                self.command(
                    NurseryAction.REVIEW_NAME_CANDIDATES,
                    actor=actor,
                    payload={"preferences": preferences},
                )
            )
        await self.service.submit(
            self.command(
                NurseryAction.SELECT_DRAFT_NAME,
                payload={"candidate_id": selected},
            )
        )
        for actor, option in ((self.user, "middle"), (self.external, "high")):
            await self.service.submit(
                self.command(
                    NurseryAction.SUBMIT_TEMPERAMENT_QUESTIONNAIRE,
                    actor=actor,
                    private_payload={
                        "answers": {
                            question_id: option for question_id in TEMPERAMENT_QUESTIONS
                        }
                    },
                )
            )
            await self.service.submit(
                self.command(
                    NurseryAction.SUBMIT_INITIAL_STYLE,
                    actor=actor,
                    private_payload={
                        "answers": {
                            question_id: "middle"
                            for question_id in INITIAL_STYLE_QUESTIONS
                        }
                    },
                )
            )
        await self.service.submit(
            self.command(
                NurseryAction.SAVE_INITIAL_SPACE,
                payload={
                    "space": {
                        "room_overall": "安静的小房间",
                        "window": "窗边暂时留空",
                    }
                },
            )
        )

    async def activate(self):
        version = self.queries.get_draft(
            actor=self.user, child_id=self.child_id
        )["draft_version"]
        first = await self.service.submit(
            self.command(
                NurseryAction.CONFIRM_CREATION,
                payload={"subject_version": version},
            )
        )
        second = await self.service.submit(
            self.command(
                NurseryAction.CONFIRM_CREATION,
                actor=self.external,
                payload={"subject_version": version},
            )
        )
        return first, second

    async def test_atomic_bootstrap_replays_same_child_and_independent_nature(self):
        operation_id = str(uuid.uuid4())
        source = self.source(self.user)
        first, second = await asyncio.gather(
            self.service.start_draft(
                actor=self.user,
                operation_id=operation_id,
                idempotency_key="same-start",
                source=source,
                display_name="用户",
            ),
            self.service.start_draft(
                actor=self.user,
                operation_id=operation_id,
                idempotency_key="same-start",
                source=source,
                display_name="用户",
            ),
        )
        self.child_id = first.result["child_id"]
        self.assertEqual(first.result["child_id"], second.result["child_id"])
        self.assertEqual(self.store.count_rows("nursery_operations"), 1)
        with closing(sqlite3.connect(self.db_path)) as connection:
            raw = connection.execute(
                "SELECT independent_temperament_json FROM nursery_creation_drafts"
            ).fetchone()[0]
        self.assertEqual(json.loads(raw), independent_temperament(self.child_id))

    async def test_full_creation_requires_two_confirmations_and_builds_one_child(self):
        await self.start()
        await self.make_ready_for_confirmation()
        first, second = await self.activate()
        self.assertEqual(first.module_state, ModuleState.DRAFT)
        self.assertFalse(first.result["confirmation_complete"])
        self.assertEqual(second.module_state, ModuleState.ACTIVE)
        self.assertTrue(second.result["confirmation_complete"])
        identity = self.store.get_child_identity(self.child_id)
        self.assertEqual(identity["official_name"], "安禾")
        self.assertEqual(identity["nickname"], "小满")
        self.assertEqual(len(identity["temperament"]), 8)
        relationships = self.store.relationships_for(self.child_id)
        self.assertEqual(len(relationships), 2)
        for relationship in relationships:
            self.assertEqual(
                (
                    relationship["trust"],
                    relationship["familiarity"],
                    relationship["closeness"],
                    relationship["repair"],
                ),
                (0.30, 0.40, 0.20, 0.00),
            )
        self.assertEqual(len(self.store.lifecycle_events_for(self.child_id)), 1)
        self.assertEqual(
            self.store.lifecycle_events_for(self.child_id)[0]["event_type"],
            "child_created",
        )
        self.assertEqual(
            self.store.lifecycle_events_for(self.child_id)[0]["sync_status"],
            "pending",
        )
        with closing(sqlite3.connect(self.db_path)) as connection:
            name_history = connection.execute(
                "SELECT name_kind, name_value FROM nursery_name_history "
                "WHERE child_id=? ORDER BY name_kind",
                (self.child_id,),
            ).fetchall()
        self.assertEqual(
            name_history, [("nickname", "小满"), ("official", "安禾")]
        )

    async def test_raw_questionnaires_are_private_then_purged_on_activation(self):
        await self.start()
        await self.make_ready_for_confirmation()
        draft = self.queries.get_draft(actor=self.external, child_id=self.child_id)
        encoded = json.dumps(draft, ensure_ascii=False)
        for forbidden in (
            "private_ref",
            "answer_digest",
            "normalized_json",
            "independent_temperament",
            "final_vector",
            "credential_ref",
        ):
            self.assertNotIn(forbidden, encoded)
        questionnaire_refs = [
            ref for ref in self.vault._values if ref.startswith("questionnaire:")
        ]
        self.assertEqual(len(questionnaire_refs), 4)
        await self.activate()
        self.assertFalse(
            any(ref.startswith("questionnaire:") for ref in self.vault._values)
        )
        with closing(sqlite3.connect(self.db_path)) as connection:
            refs = {
                row[0]
                for row in connection.execute(
                    "SELECT private_ref FROM nursery_private_submissions"
                )
            }
        self.assertEqual(refs, {"purged"})

    async def test_model_credential_never_enters_sqlite_or_operation_payload(self):
        await self.start()
        await self.bind_external()
        secret = "test-key-UNIQUE-STAGE2-CREDENTIAL-7291"
        result = await self.save_model(secret)
        self.assertEqual(result.status, OperationStatus.COMPLETED)
        with closing(sqlite3.connect(self.db_path)) as connection:
            payloads = [
                row[0]
                for row in connection.execute(
                    "SELECT payload_json FROM nursery_operations"
                )
            ]
        self.assertTrue(all(secret not in payload for payload in payloads))
        database_bytes = b"".join(
            path.read_bytes()
            for path in self.db_path.parent.glob("nursery.sqlite3*")
            if path.is_file()
        )
        self.assertNotIn(secret.encode("utf-8"), database_bytes)
        view = self.queries.get_draft(actor=self.external, child_id=self.child_id)
        self.assertNotIn("credential_ref", view["model_connection"])
        self.assertEqual(view["model_connection"]["credential_suffix"], "7291")

    async def test_public_operation_payload_rejects_secrets_before_persistence(self):
        await self.start()
        before = self.store.count_rows("nursery_operations")
        with self.assertRaises(NurseryError) as private:
            await self.service.submit(
                self.command(
                    NurseryAction.SAVE_MODEL_CONNECTION,
                    payload={
                        "provider_id": "fake",
                        "base_url": "https://models.example/v1",
                        "model_name": "child-model",
                        "credential": "must-not-persist",
                    },
                )
            )
        self.assertEqual(private.exception.code, "PRIVATE_DATA_IN_PUBLIC_PAYLOAD")
        self.assertEqual(self.store.count_rows("nursery_operations"), before)

    async def test_insecure_remote_model_url_is_rejected_without_call_or_secret(self):
        await self.start()
        await self.bind_external()
        result = await self.service.submit(
            self.command(
                NurseryAction.SAVE_MODEL_CONNECTION,
                payload={
                    "provider_id": "fake",
                    "base_url": "http://models.example/v1",
                    "model_name": "child-model",
                },
                private_payload={"credential": "sk-insecure-4444"},
            )
        )
        self.assertEqual(result.status, OperationStatus.REJECTED)
        self.assertEqual(result.error_code, "INSECURE_MODEL_URL")
        self.assertEqual(self.adapter.calls, 0)
        self.assertFalse(self.vault._values)

    async def test_external_guardian_cannot_change_model_or_bind_guardian(self):
        await self.start()
        await self.bind_external()
        with self.assertRaises(NurseryError) as model:
            await self.service.submit(
                self.command(
                    NurseryAction.SAVE_MODEL_CONNECTION,
                    actor=self.external,
                    payload={
                        "provider_id": "fake",
                        "base_url": "https://models.example/v1",
                        "model_name": "child-model",
                    },
                    private_payload={"credential": "sk-forbidden-5555"},
                )
            )
        self.assertEqual(model.exception.code, "USER_GUARDIAN_REQUIRED")

    async def test_model_replacement_after_activation_keeps_child_identity(self):
        await self.start()
        await self.make_ready_for_confirmation()
        await self.activate()
        identity_before = self.store.get_child_identity(self.child_id)
        old_ref = self.store.get_model_config_internal("account")["credential_ref"]
        replaced = await self.save_model("test-key-replacement-6666")
        self.assertEqual(replaced.status, OperationStatus.COMPLETED)
        self.assertEqual(self.store.get_child_identity(self.child_id), identity_before)
        self.assertFalse(self.vault.exists(old_ref))
        current = self.store.get_model_config_internal("account")
        self.assertEqual(current["credential_suffix"], "6666")
        self.assertTrue(self.vault.exists(current["credential_ref"]))

    async def test_failed_model_replacement_preserves_working_configuration(self):
        await self.start()
        await self.bind_external()
        await self.save_model("sk-working-1111")
        old = self.store.get_model_config_internal("account")
        self.adapter.fail = True
        failed = await self.save_model("sk-failing-2222")
        self.assertEqual(failed.status, OperationStatus.FAILED_RETRYABLE)
        current = self.store.get_model_config_internal("account")
        self.assertEqual(current["credential_ref"], old["credential_ref"])
        self.assertTrue(self.vault.exists(old["credential_ref"]))
        self.assertFalse(any("2222" in value for value in self.vault._values.values()))

    async def test_retest_uses_saved_secret_and_failure_preserves_ready_config(self):
        await self.start()
        await self.bind_external()
        await self.save_model("test-key-retest-existing-1212")
        original = self.store.get_model_config_internal("account")
        retested = await self.service.submit(
            self.command(
                NurseryAction.RETEST_MODEL_CONNECTION,
                payload={"timeout_seconds": 8},
            )
        )
        self.assertEqual(retested.status, OperationStatus.COMPLETED)
        current = self.store.get_model_config_internal("account")
        self.assertEqual(current["credential_ref"], original["credential_ref"])
        self.assertEqual(current["config_version"], original["config_version"])
        self.adapter.fail = True
        failed = await self.service.submit(
            self.command(
                NurseryAction.RETEST_MODEL_CONNECTION,
                payload={"timeout_seconds": 8},
            )
        )
        self.assertEqual(failed.status, OperationStatus.FAILED_RETRYABLE)
        after_failure = self.store.get_model_config_internal("account")
        self.assertEqual(after_failure["credential_ref"], original["credential_ref"])
        self.assertTrue(self.vault.exists(original["credential_ref"]))

    async def test_delete_model_connection_removes_secret_but_keeps_draft(self):
        await self.start()
        await self.bind_external()
        await self.save_model("test-key-delete-existing-3434")
        reference = self.store.get_model_config_internal("account")["credential_ref"]
        deleted = await self.service.submit(
            self.command(NurseryAction.DELETE_MODEL_CONNECTION)
        )
        self.assertEqual(deleted.status, OperationStatus.COMPLETED)
        self.assertEqual(deleted.result["model_connection"], "deleted")
        self.assertIsNone(self.store.get_model_config_internal("account"))
        self.assertFalse(self.vault.exists(reference))
        self.assertEqual(
            self.store.get_state("account", self.child_id)["module_state"], "draft"
        )
        view = self.queries.get_draft(actor=self.user, child_id=self.child_id)
        self.assertIn("model_connection", view["missing_requirements"])

    async def test_concurrent_model_replay_tests_and_saves_once(self):
        await self.start()
        await self.bind_external()
        # Exceed the process-local lock wait so duplicate callers must follow
        # the replay settlement path instead of observing a transient status.
        self.adapter.delay_seconds = 0.35
        command = self.command(
            NurseryAction.SAVE_MODEL_CONNECTION,
            payload={
                "provider_id": "fake",
                "base_url": "https://models.example/v1",
                "model_name": "child-model",
                "timeout_seconds": 5,
            },
            private_payload={"credential": "sk-concurrent-3333"},
        )
        results = await asyncio.gather(
            *(self.service.submit(command) for _ in range(20))
        )
        self.assertTrue(all(item.status == OperationStatus.COMPLETED for item in results))
        self.assertEqual(self.adapter.calls, 1)
        self.assertEqual(self.vault.put_calls, 1)

    async def test_name_without_two_acceptances_cannot_be_selected(self):
        await self.start()
        await self.bind_external()
        proposals = self.proposals(self.user, ["安禾"])
        await self.service.submit(
            self.command(
                NurseryAction.SAVE_NAME_PROPOSALS,
                payload={"proposals": proposals},
            )
        )
        candidate_id = proposals[0]["candidate_id"]
        await self.service.submit(
            self.command(
                NurseryAction.REVIEW_NAME_CANDIDATES,
                payload={"preferences": {candidate_id: "like"}},
            )
        )
        await self.service.submit(
            self.command(
                NurseryAction.REVIEW_NAME_CANDIDATES,
                actor=self.external,
                payload={"preferences": {candidate_id: "reject"}},
            )
        )
        selected = await self.service.submit(
            self.command(
                NurseryAction.SELECT_DRAFT_NAME,
                payload={"candidate_id": candidate_id},
            )
        )
        self.assertEqual(selected.status, OperationStatus.REJECTED)
        self.assertEqual(selected.error_code, "NAME_CONSENSUS_REQUIRED")

    async def test_unique_shared_name_is_selected_when_external_guardian_votes_last(self):
        await self.start()
        await self.bind_external()
        proposals = self.proposals(self.user, ["安禾"])
        await self.service.submit(
            self.command(
                NurseryAction.SAVE_NAME_PROPOSALS,
                payload={"proposals": proposals},
            )
        )
        candidate_id = proposals[0]["candidate_id"]
        await self.service.submit(
            self.command(
                NurseryAction.REVIEW_NAME_CANDIDATES,
                payload={"preferences": {candidate_id: "like"}},
            )
        )
        result = await self.service.submit(
            self.command(
                NurseryAction.REVIEW_NAME_CANDIDATES,
                actor=self.external,
                payload={"preferences": {candidate_id: "like"}},
            )
        )
        draft = self.queries.get_draft(actor=self.user, child_id=self.child_id)
        self.assertEqual(result.result["official_name"], "安禾")
        self.assertTrue(result.result["name_auto_selected"])
        self.assertEqual(draft["selected_candidate_id"], candidate_id)
        self.assertEqual(draft["official_name"], "安禾")
        self.assertNotIn("agreed_name", draft["missing_requirements"])

    async def test_unique_shared_name_is_selected_when_user_votes_last_and_disagree_maps_to_reject(self):
        await self.start()
        await self.bind_external()
        proposals = self.proposals(self.user, ["安禾", "宁宁"])
        await self.service.submit(
            self.command(
                NurseryAction.SAVE_NAME_PROPOSALS,
                payload={"proposals": proposals},
            )
        )
        preferences = {
            proposals[0]["candidate_id"]: "like",
            proposals[1]["candidate_id"]: "disagree",
        }
        await self.service.submit(
            self.command(
                NurseryAction.REVIEW_NAME_CANDIDATES,
                actor=self.external,
                payload={"preferences": preferences},
            )
        )
        await self.service.submit(
            self.command(
                NurseryAction.REVIEW_NAME_CANDIDATES,
                payload={"preferences": preferences},
            )
        )
        draft = self.queries.get_draft(actor=self.user, child_id=self.child_id)
        self.assertEqual(draft["official_name"], "安禾")
        recorded = {
            (item["reviewer_id"], item["candidate_id"]): item["preference"]
            for item in draft["name_preferences"]
        }
        self.assertEqual(
            recorded[(self.external.caregiver_id, proposals[1]["candidate_id"])],
            "reject",
        )

    async def test_multiple_shared_names_wait_for_explicit_selection_by_external_guardian(self):
        await self.start()
        await self.bind_external()
        proposals = self.proposals(self.user, ["安禾", "宁宁"])
        await self.service.submit(
            self.command(
                NurseryAction.SAVE_NAME_PROPOSALS,
                payload={"proposals": proposals},
            )
        )
        preferences = {item["candidate_id"]: "acceptable" for item in proposals}
        for actor in (self.user, self.external):
            await self.service.submit(
                self.command(
                    NurseryAction.REVIEW_NAME_CANDIDATES,
                    actor=actor,
                    payload={"preferences": preferences},
                )
            )
        draft = self.queries.get_draft(actor=self.user, child_id=self.child_id)
        self.assertIsNone(draft["selected_candidate_id"])
        self.assertIn("agreed_name", draft["missing_requirements"])
        selected = await ExternalNurseryTools(
            store=self.store, creation=self.service
        ).select_name(
            actor=self.external,
            candidate_id=proposals[1]["candidate_id"],
        )
        self.assertEqual(selected["status"], OperationStatus.COMPLETED.value)
        draft = self.queries.get_draft(actor=self.user, child_id=self.child_id)
        self.assertEqual(draft["official_name"], "宁宁")

        # Later reviews that preserve both acceptances must not erase the
        # explicit choice merely because several shared candidates remain.
        await self.service.submit(
            self.command(
                NurseryAction.REVIEW_NAME_CANDIDATES,
                actor=self.external,
                payload={"preferences": preferences},
            )
        )
        draft = self.queries.get_draft(actor=self.user, child_id=self.child_id)
        self.assertEqual(draft["selected_candidate_id"], proposals[1]["candidate_id"])
        self.assertEqual(draft["official_name"], "宁宁")

    async def test_set_name_preference_alias_is_accepted_by_external_adapter(self):
        await self.start()
        await self.bind_external()
        proposals = self.proposals(self.user, ["安禾"])
        await self.service.submit(
            self.command(
                NurseryAction.SAVE_NAME_PROPOSALS,
                payload={"proposals": proposals},
            )
        )
        candidate_id = proposals[0]["candidate_id"]
        result = await ExternalNurseryTools(
            store=self.store, creation=self.service
        ).creation_submit(
            actor=self.external,
            child_id=None,
            action="set_name_preference",
            payload={"preferences": {candidate_id: "disagree"}},
        )
        self.assertEqual(result["status"], OperationStatus.COMPLETED.value)
        draft = self.queries.get_draft(actor=self.user, child_id=self.child_id)
        self.assertEqual(draft["name_preferences"][0]["preference"], "reject")

    async def test_changing_review_clears_old_selection_and_replacing_reviewed_candidates_is_safe(self):
        await self.start()
        await self.bind_external()
        proposals = self.proposals(self.user, ["安禾"])
        await self.service.submit(
            self.command(
                NurseryAction.SAVE_NAME_PROPOSALS,
                payload={"proposals": proposals},
            )
        )
        candidate_id = proposals[0]["candidate_id"]
        for actor in (self.user, self.external):
            await self.service.submit(
                self.command(
                    NurseryAction.REVIEW_NAME_CANDIDATES,
                    actor=actor,
                    payload={"preferences": {candidate_id: "like"}},
                )
            )
        await self.service.submit(
            self.command(
                NurseryAction.REVIEW_NAME_CANDIDATES,
                actor=self.external,
                payload={"preferences": {candidate_id: "reject"}},
            )
        )
        draft = self.queries.get_draft(actor=self.user, child_id=self.child_id)
        self.assertIsNone(draft["selected_candidate_id"])
        self.assertIsNone(draft["official_name"])
        replacement = self.proposals(self.user, ["小满"])
        replaced = await self.service.submit(
            self.command(
                NurseryAction.SAVE_NAME_PROPOSALS,
                payload={"proposals": replacement},
            )
        )
        self.assertEqual(replaced.status, OperationStatus.COMPLETED)
        draft = self.queries.get_draft(actor=self.user, child_id=self.child_id)
        self.assertEqual(
            [item["candidate_id"] for item in draft["name_candidates"]],
            [replacement[0]["candidate_id"]],
        )
        self.assertEqual(draft["name_preferences"], [])

    async def test_phone_name_proposals_get_the_server_candidate_id(self):
        await self.start()
        await self.bind_external()
        saved = await self.service.submit(
            self.command(
                NurseryAction.SAVE_NAME_PROPOSALS,
                payload={"proposals": [{"name": "安禾"}]},
            )
        )
        self.assertEqual(saved.status, OperationStatus.COMPLETED)
        draft = self.queries.get_draft(actor=self.user, child_id=self.child_id)
        self.assertEqual(
            draft["name_candidates"][0]["candidate_id"],
            name_candidate_id(self.child_id, self.user.caregiver_id, 1, "安禾"),
        )

    async def test_invalid_stage_is_rejected_from_authoritative_yaml(self):
        await self.start()
        with self.assertRaises(NurseryError) as invalid:
            await self.service.submit(
                self.command(
                    NurseryAction.SAVE_DRAFT_IDENTITY,
                    payload={
                        "sex_status": "undecided",
                        "stage_id": "school_age",
                        "nickname": "",
                        "address_terms": {},
                    },
                )
            )
        self.assertEqual(invalid.exception.code, "INVALID_STAGE")

    async def test_questionnaire_update_never_rerolls_independent_nature(self):
        await self.start()
        await self.bind_external()
        with closing(sqlite3.connect(self.db_path)) as connection:
            before = connection.execute(
                "SELECT independent_temperament_json FROM nursery_creation_drafts"
            ).fetchone()[0]
        first = self.command(
            NurseryAction.SUBMIT_TEMPERAMENT_QUESTIONNAIRE,
            private_payload={
                "answers": {
                    question_id: "low" for question_id in TEMPERAMENT_QUESTIONS
                }
            },
        )
        await self.service.submit(first)
        old_reference = self.store.get_private_submission_ref(
            child_id=self.child_id,
            caregiver_id=self.user.caregiver_id,
            questionnaire_kind="temperament",
        )
        await self.service.submit(
            self.command(
                NurseryAction.SUBMIT_TEMPERAMENT_QUESTIONNAIRE,
                private_payload={
                    "answers": {
                        question_id: "high" for question_id in TEMPERAMENT_QUESTIONS
                    }
                },
            )
        )
        with closing(sqlite3.connect(self.db_path)) as connection:
            after = connection.execute(
                "SELECT independent_temperament_json FROM nursery_creation_drafts"
            ).fetchone()[0]
        self.assertEqual(before, after)
        self.assertFalse(self.vault.exists(old_reference))

    async def test_external_ai_can_vote_and_submit_both_forms_without_protocol_ids(self):
        await self.start()
        proposals = self.proposals(self.user, ["小树"])
        await self.service.submit(
            self.command(
                NurseryAction.SAVE_NAME_PROPOSALS,
                payload={"proposals": proposals},
            )
        )
        tools = ExternalNurseryTools(store=self.store, creation=self.service)
        vote = await tools.submit_name_opinions(
            actor=self.external,
            opinions=[{"name": "小树", "opinion": "like"}],
        )
        self.assertEqual(vote["status"], OperationStatus.COMPLETED.value)
        temperament = await tools.creation_submit(
            actor=self.external,
            child_id=None,
            action="submit_questionnaire",
            payload={
                "questionnaire_kind": "temperament",
                "answers": {question_id: "middle" for question_id in TEMPERAMENT_QUESTIONS},
            },
        )
        self.assertEqual(temperament["status"], OperationStatus.COMPLETED.value)
        care_style = await tools.submit_care_style_answers(
            actor=self.external,
            answers={question_id: "high" for question_id in INITIAL_STYLE_QUESTIONS},
        )
        self.assertEqual(care_style["status"], OperationStatus.COMPLETED.value)
        self.assertIsNotNone(
            self.store.get_private_submission_ref(
                child_id=self.child_id,
                caregiver_id=self.external.caregiver_id,
                questionnaire_kind="temperament",
            )
        )
        self.assertIsNotNone(
            self.store.get_private_submission_ref(
                child_id=self.child_id,
                caregiver_id=self.external.caregiver_id,
                questionnaire_kind="initial_style",
            )
        )

    async def test_draft_change_invalidates_old_creation_confirmation_version(self):
        await self.start()
        await self.make_ready_for_confirmation()
        version = self.queries.get_draft(
            actor=self.user, child_id=self.child_id
        )["draft_version"]
        first = await self.service.submit(
            self.command(
                NurseryAction.CONFIRM_CREATION,
                payload={"subject_version": version},
            )
        )
        self.assertFalse(first.result["confirmation_complete"])
        await self.service.submit(
            self.command(
                NurseryAction.SAVE_INITIAL_SPACE,
                payload={"space": {"room_overall": "修改后的房间"}},
            )
        )
        stale = await self.service.submit(
            self.command(
                NurseryAction.CONFIRM_CREATION,
                actor=self.external,
                payload={"subject_version": version},
            )
        )
        self.assertEqual(stale.status, OperationStatus.REJECTED)
        self.assertEqual(stale.error_code, "CONFIRMATION_VERSION_CONFLICT")
        self.assertEqual(
            self.store.get_state("account", self.child_id)["module_state"], "draft"
        )

    async def test_non_founding_companion_cannot_read_creation_draft(self):
        await self.start()
        companion = guardian("later-companion", CaregiverRole.COMPANION)
        self.store.register_caregiver(companion)
        with self.assertRaises(NurseryError) as forbidden:
            self.queries.get_draft(actor=companion, child_id=self.child_id)
        self.assertEqual(forbidden.exception.code, "ACTOR_NOT_GUARDIAN")

    async def test_creation_confirmation_fails_closed_when_incomplete(self):
        await self.start()
        await self.bind_external()
        draft = self.queries.get_draft(actor=self.user, child_id=self.child_id)
        result = await self.service.submit(
            self.command(
                NurseryAction.CONFIRM_CREATION,
                payload={"subject_version": draft["draft_version"]},
            )
        )
        self.assertEqual(result.status, OperationStatus.REJECTED)
        self.assertEqual(result.error_code, "CREATION_NOT_READY")
        self.assertEqual(
            self.store.get_state("account", self.child_id)["module_state"], "draft"
        )

    async def test_creation_requires_resolved_boy_or_girl(self):
        await self.start()
        await self.make_ready_for_confirmation()
        await self.service.submit(
            self.command(
                NurseryAction.SAVE_DRAFT_IDENTITY,
                payload={
                    "sex_status": "neutral",
                    "stage_id": "infancy",
                    "nickname": "小满",
                    "address_terms": {},
                },
            )
        )
        draft = self.queries.get_draft(actor=self.user, child_id=self.child_id)
        self.assertIn("resolved_sex", draft["missing_requirements"])
        result = await self.service.submit(
            self.command(
                NurseryAction.CONFIRM_CREATION,
                payload={"subject_version": draft["draft_version"]},
            )
        )
        self.assertEqual(result.status, OperationStatus.REJECTED)
        self.assertEqual(result.error_code, "CREATION_NOT_READY")

    async def test_draft_reads_are_side_effect_free(self):
        await self.start()
        await self.bind_external()
        before_operations = self.store.count_rows("nursery_operations")
        before_version = self.store.get_state("account", self.child_id)["state_version"]
        for _ in range(20):
            self.queries.get_draft(actor=self.external, child_id=self.child_id)
        self.assertEqual(self.store.count_rows("nursery_operations"), before_operations)
        self.assertEqual(
            self.store.get_state("account", self.child_id)["state_version"],
            before_version,
        )
        self.assertEqual(self.adapter.calls, 0)

    async def test_replayed_final_confirmation_cannot_duplicate_birth(self):
        await self.start()
        await self.make_ready_for_confirmation()
        version = self.queries.get_draft(
            actor=self.user, child_id=self.child_id
        )["draft_version"]
        await self.service.submit(
            self.command(
                NurseryAction.CONFIRM_CREATION,
                payload={"subject_version": version},
            )
        )
        command = self.command(
            NurseryAction.CONFIRM_CREATION,
            actor=self.external,
            payload={"subject_version": version},
        )
        first = await self.service.submit(command)
        replay = await self.service.submit(command)
        self.assertEqual(first.status, OperationStatus.COMPLETED)
        self.assertTrue(replay.replayed)
        self.assertEqual(len(self.store.lifecycle_events_for(self.child_id)), 1)
        self.assertEqual(len(self.store.relationships_for(self.child_id)), 2)


class SecretVaultTests(unittest.TestCase):
    def test_file_vault_encrypts_at_rest_and_requires_matching_key(self):
        with tempfile.TemporaryDirectory() as root:
            key = FernetFileSecretVault.generate_key()
            vault = FernetFileSecretVault(root, key)
            value = "test-key-plaintext-must-not-appear-8842"
            vault.put("model:account:operation", value)
            stored = b"".join(path.read_bytes() for path in Path(root).iterdir())
            self.assertNotIn(value.encode("utf-8"), stored)
            self.assertEqual(vault.get("model:account:operation"), value)
            wrong = FernetFileSecretVault(root, FernetFileSecretVault.generate_key())
            with self.assertRaises(NurseryError) as invalid:
                wrong.get("model:account:operation")
            self.assertEqual(invalid.exception.code, "SECRET_DECRYPTION_FAILED")

    def test_openai_compatible_adapter_checks_structured_chinese_output(self):
        captured = {}

        class Response:
            class Choice:
                class Message:
                    content = '{"ready": true, "reply": "连接测试通过"}'

                message = Message()

            choices = [Choice()]

        class Completions:
            def create(self, **kwargs):
                captured["request"] = kwargs
                return Response()

        class Client:
            class Chat:
                completions = Completions()

            chat = Chat()

        def factory(**kwargs):
            captured["client"] = kwargs
            return Client()

        adapter = OpenAICompatibleChildModelAdapter(factory)
        result = adapter.test_connection(
            ModelConnectionRequest(
                provider_id="openai_compatible",
                base_url="https://models.example/v1",
                model_name="child-model",
                credential="test-key-adapter-test-7777",
            )
        )
        self.assertTrue(result.ready)
        self.assertEqual(captured["client"]["api_key"], "test-key-adapter-test-7777")
        self.assertEqual(captured["request"]["response_format"], {"type": "json_object"})


if __name__ == "__main__":
    unittest.main()
