from __future__ import annotations

import asyncio
import json
import os
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx
from starlette.applications import Starlette
from starlette.routing import Route
from starlette.testclient import TestClient

import manager_server
from nursery.capability_rules import load_capability_rules
from nursery.creation_queries import NurseryCreationQueryService
from nursery.creation_rules import INITIAL_STYLE_QUESTIONS, TEMPERAMENT_QUESTIONS
from nursery.creation_service import NurseryCreationService
from nursery.external_tools import ExternalNurseryTools
from nursery.feature_service import NurseryFeatureService
from nursery.interaction_service import NurseryInteractionService
from nursery.http_api import (
    DB_PATH_ENV,
    INTERNAL_TOKEN_ENV,
    NurseryHTTPRuntime,
    NurseryInternalHTTPAPI,
    NurseryRuntimeProvider,
    RULES_PATH_ENV,
    VAULT_DIR_ENV,
    VAULT_KEY_ENV,
)
from nursery.mcp_tools import BoundExternalMCPActorResolver, register_external_nursery_mcp_tools
from nursery.model_connection import (
    AdapterRegistry,
    ChildConversationProposal,
    ChildFamilyEventProposal,
    ModelCapabilities,
)
from nursery.models import ActorContext, AuthSource, CaregiverRole, ModuleState, SourceType
from nursery.secret_vault import FernetFileSecretVault, InMemorySecretVault
from nursery.store import NurseryStore


RULES_PATH = Path(__file__).resolve().parent / "nursery" / "capability-rules-v1.yaml"
INTERNAL_TOKEN = "nursery-internal-test-token-1234567890"


class FakeModelAdapter:
    def __init__(self) -> None:
        self.calls = 0

    def test_connection(self, request):
        self.calls += 1
        return ModelCapabilities(True, True, True, True)

    def respond(self, request, conversation):
        self.calls += 1
        state = {
            key: float(conversation.runtime_state.get(key, 0.0))
            for key in (
                "thirst",
                "hunger",
                "fatigue",
                "comfort",
                "connection",
                "play_drive",
                "unwell",
            )
        }
        state["connection"] = min(1.0, float(state.get("connection", 0.5)) + 0.1)
        return ChildConversationProposal(
            reply="我听见啦，想和你再说一会儿。",
            intent="talk",
            runtime_state=state,
        )

    def respond_to_family_event(self, request, event):
        self.calls += 1
        return ChildFamilyEventProposal(
            child_reaction="我听见家里有一点变化。",
            current_ripple="想再靠近一点。",
        )


class FakeMCP:
    def __init__(self) -> None:
        self.functions = {}

    def tool(self):
        def register(function):
            self.functions[function.__name__] = function
            return function

        return register


def make_app(api: NurseryInternalHTTPAPI) -> Starlette:
    return Starlette(
        routes=[
            Route(
                "/internal/nursery/anima/family-events",
                api.apply_anima_family_event,
                methods=["POST"],
            ),
            Route(
                "/internal/nursery/anima/family-events/supersede",
                api.supersede_anima_family_event,
                methods=["POST"],
            ),
            Route(
                "/internal/nursery/anima/family-events/revoke",
                api.revoke_anima_family_event,
                methods=["POST"],
            ),
            Route("/internal/nursery/user/status", api.status, methods=["GET"]),
            Route(
                "/internal/nursery/user/entry-preference",
                api.get_entry_preference,
                methods=["GET"],
            ),
            Route(
                "/internal/nursery/user/entry-preference",
                api.update_entry_preference,
                methods=["PUT"],
            ),
            Route(
                "/internal/nursery/user/drafts",
                api.start_draft,
                methods=["POST"],
            ),
            Route(
                "/internal/nursery/user/drafts/{child_id}",
                api.get_draft,
                methods=["GET"],
            ),
            Route(
                "/internal/nursery/user/drafts/{child_id}/operations",
                api.submit_operation,
                methods=["POST"],
            ),
            Route(
                "/internal/nursery/user/operations/{operation_id}",
                api.get_operation,
                methods=["GET"],
            ),
            Route(
                "/internal/nursery/user/children/{child_id}/interactions",
                api.child_interact,
                methods=["POST"],
            ),
            Route(
                "/internal/nursery/user/children/{child_id}/status",
                api.child_status,
                methods=["GET"],
            ),
            Route(
                "/internal/nursery/user/children/{child_id}/operations",
                api.submit_feature_operation,
                methods=["POST"],
            ),
        ]
    )


class NurseryInternalHTTPTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp.name) / "nursery.sqlite3"
        self.store = NurseryStore(self.db_path)
        self.vault = InMemorySecretVault()
        self.adapter = FakeModelAdapter()
        registry = AdapterRegistry()
        registry.register("fake", self.adapter)
        actor = ActorContext.build(
            account_id="account",
            caregiver_id="user-guardian",
            role=CaregiverRole.USER_GUARDIAN,
            permissions=("nursery.*",),
            auth_source=AuthSource.MANAGER_SESSION,
        )
        self.runtime = NurseryHTTPRuntime(
            store=self.store,
            creation=NurseryCreationService(
                self.store,
                vault=self.vault,
                capability_rules=load_capability_rules(RULES_PATH),
                adapter_registry=registry,
            ),
            drafts=NurseryCreationQueryService(self.store),
            interaction=NurseryInteractionService(
                self.store,
                vault=self.vault,
                adapter_registry=registry,
            ),
            feature=NurseryFeatureService(self.store),
            user_actor=actor,
        )
        self.provider = NurseryRuntimeProvider(
            {"buckets_dir": self.temp.name},
            environment={
                INTERNAL_TOKEN_ENV: INTERNAL_TOKEN,
            },
        )
        self.provider._runtime = self.runtime
        self.client = TestClient(make_app(NurseryInternalHTTPAPI(self.provider)))
        self.headers = {"x-anima-internal-token": INTERNAL_TOKEN}

    def tearDown(self):
        self.client.close()
        self.temp.cleanup()

    def activate_family_event_fixture(self) -> None:
        self.store.create_fixture(
            account_id="account",
            child_id="child",
            state=ModuleState.ACTIVE,
        )
        with self.store._write() as connection:
            connection.execute(
                """
                INSERT INTO nursery_model_configs (
                    account_id, provider_id, base_url, model_name, credential_ref,
                    credential_suffix, connection_status, capabilities_json,
                    config_version, tested_at, created_at, updated_at
                ) VALUES ('account', 'fake', 'http://localhost:8001', 'child-model',
                    'model:account', 'test', 'ready', '{}', 1, 'now', 'now', 'now')
                """
            )
        self.vault.put("model:account", "test-secret")

    @staticmethod
    def family_event(version: str = "v1") -> dict:
        return {
            "source_key": "mailbox:42",
            "source_version": version,
            "category": "relationship",
            "child_safe_summary": "家里刚刚温和地把一件事说开了。",
            "occurred_at": "2026-09-03T00:00:00+00:00",
        }

    def test_family_event_routes_authenticate_validate_and_resolve_owned_child(self):
        self.activate_family_event_fixture()
        rejected = self.client.post(
            "/internal/nursery/anima/family-events",
            json={"event": self.family_event()},
        )
        self.assertEqual(rejected.status_code, 401)
        unsafe = self.client.post(
            "/internal/nursery/anima/family-events",
            headers=self.headers,
            json={"event": {**self.family_event(), "content": "adult raw text"}},
        )
        self.assertEqual(unsafe.status_code, 422)
        self.assertEqual(unsafe.json()["error"]["code"], "UNKNOWN_REQUEST_FIELD")
        forged = self.client.post(
            "/internal/nursery/anima/family-events",
            headers=self.headers,
            json={"event": self.family_event(), "child_id": "someone-else"},
        )
        self.assertEqual(forged.status_code, 422)
        self.assertEqual(forged.json()["error"]["code"], "UNKNOWN_REQUEST_FIELD")

        first = self.client.post(
            "/internal/nursery/anima/family-events",
            headers=self.headers,
            json={"event": self.family_event()},
        )
        replay = self.client.post(
            "/internal/nursery/anima/family-events",
            headers=self.headers,
            json={"event": self.family_event()},
        )
        self.assertEqual(first.status_code, 200, first.text)
        self.assertEqual(replay.status_code, 200, replay.text)
        self.assertEqual(self.adapter.calls, 1)
        with self.store._read() as connection:
            source = connection.execute(
                """
                SELECT * FROM nursery_source_events
                WHERE account_id='account' AND child_id='child'
                    AND source_type='mailbox_event' AND source_id='mailbox:42'
                    AND source_version='v1'
                """
            ).fetchone()
        self.assertEqual(source["account_id"], "account")
        self.assertEqual(source["child_id"], "child")
        self.assertEqual(source["source_type"], SourceType.MAILBOX_EVENT.value)

    def test_family_event_correction_routes_are_scoped_to_runtime_child(self):
        self.activate_family_event_fixture()
        applied = self.client.post(
            "/internal/nursery/anima/family-events",
            headers=self.headers,
            json={"event": self.family_event()},
        )
        self.assertEqual(applied.status_code, 200, applied.text)
        old = self.family_event()
        self.store.create_fixture(
            account_id="other-account",
            child_id="other-child",
            state=ModuleState.ACTIVE,
        )
        with self.store._write() as connection:
            connection.execute(
                """
                INSERT INTO nursery_experiences (
                    experience_id, child_id, category, occurred_at,
                    age_appropriate_summary, child_reaction, resolution,
                    requires_attention, created_at, updated_at
                ) VALUES ('other-experience', 'other-child', 'family', 'now',
                    'other safe summary', 'other reaction', 'unresolved', 0, 'now', 'now')
                """
            )
            connection.execute(
                """
                INSERT INTO nursery_experience_sources (
                    experience_id, source_type, source_id, source_version, source_validity
                ) VALUES ('other-experience', 'mailbox_event', 'mailbox:42', 'v1', 'valid')
                """
            )
        superseded = self.client.post(
            "/internal/nursery/anima/family-events/supersede",
            headers=self.headers,
            json={
                "source_key": old["source_key"],
                "source_version": old["source_version"],
                "superseded_by_source_version": "v2",
            },
        )
        self.assertEqual(superseded.status_code, 200, superseded.text)
        with self.store._read() as connection:
            other_experience = connection.execute(
                "SELECT resolution FROM nursery_experiences WHERE experience_id='other-experience'"
            ).fetchone()
            other_source = connection.execute(
                "SELECT source_validity FROM nursery_experience_sources WHERE experience_id='other-experience'"
            ).fetchone()
        self.assertEqual(other_experience["resolution"], "unresolved")
        self.assertEqual(other_source["source_validity"], "valid")
        revoked = self.client.post(
            "/internal/nursery/anima/family-events/revoke",
            headers=self.headers,
            json={"source_key": old["source_key"], "source_version": "v1"},
        )
        self.assertEqual(revoked.status_code, 200, revoked.text)
        with self.store._read() as connection:
            other_experience = connection.execute(
                "SELECT resolution FROM nursery_experiences WHERE experience_id='other-experience'"
            ).fetchone()
            other_source = connection.execute(
                "SELECT source_validity FROM nursery_experience_sources WHERE experience_id='other-experience'"
            ).fetchone()
        self.assertEqual(other_experience["resolution"], "unresolved")
        self.assertEqual(other_source["source_validity"], "valid")

    def start_draft(self) -> tuple[str, str]:
        operation_id = str(uuid.uuid4())
        response = self.client.post(
            "/internal/nursery/user/drafts",
            headers=self.headers,
            json={
                "operation_id": operation_id,
                "idempotency_key": "start-draft",
                "display_name": "用户",
            },
        )
        self.assertEqual(response.status_code, 200, response.text)
        child_id = response.json()["operation"]["result"]["child_id"]
        return child_id, operation_id

    def operation(self, child_id: str, action: str, **extra):
        body = {
            "action": action,
            "operation_id": str(uuid.uuid4()),
            "idempotency_key": f"idem-{uuid.uuid4()}",
            "expected_state_version": None,
            "payload": {},
        }
        body.update(extra)
        return self.client.post(
            f"/internal/nursery/user/drafts/{child_id}/operations",
            headers=self.headers,
            json=body,
        )

    def test_authentication_happens_before_lazy_storage_initialization(self):
        with tempfile.TemporaryDirectory() as root:
            provider = NurseryRuntimeProvider(
                {"buckets_dir": root},
                environment={INTERNAL_TOKEN_ENV: INTERNAL_TOKEN},
            )
            client = TestClient(make_app(NurseryInternalHTTPAPI(provider)))
            try:
                rejected = client.get("/internal/nursery/user/status")
                self.assertEqual(rejected.status_code, 401)
                self.assertFalse((Path(root) / "nursery.sqlite3").exists())
                unconfigured = client.get(
                    "/internal/nursery/user/status", headers=self.headers
                )
                self.assertEqual(unconfigured.status_code, 503)
                self.assertFalse((Path(root) / "nursery.sqlite3").exists())
            finally:
                client.close()

    def test_entry_preference_is_account_level_and_does_not_start_nursery(self):
        initial = self.client.get(
            "/internal/nursery/user/entry-preference", headers=self.headers
        )
        self.assertEqual(initial.status_code, 200, initial.text)
        self.assertEqual(initial.json()["preference"], "undecided")

        hidden = self.client.put(
            "/internal/nursery/user/entry-preference",
            headers=self.headers,
            json={"preference": "hidden"},
        )
        self.assertEqual(hidden.status_code, 200, hidden.text)
        self.assertEqual(hidden.json()["preference"], "hidden")
        self.assertIsNotNone(hidden.json()["updated_at"])
        self.assertEqual(
            self.store.get_account_overview("account")["module_state"],
            "never_enabled",
        )

        visible = self.client.put(
            "/internal/nursery/user/entry-preference",
            headers=self.headers,
            json={"preference": "visible"},
        )
        self.assertEqual(visible.status_code, 200, visible.text)
        self.assertEqual(
            self.client.get(
                "/internal/nursery/user/entry-preference", headers=self.headers
            ).json()["preference"],
            "visible",
        )

    def test_entry_preference_rejects_unknown_values_and_fields(self):
        invalid = self.client.put(
            "/internal/nursery/user/entry-preference",
            headers=self.headers,
            json={"preference": "undecided"},
        )
        self.assertEqual(invalid.status_code, 422)
        self.assertEqual(invalid.json()["error"]["code"], "INVALID_ENTRY_PREFERENCE")

        unknown = self.client.put(
            "/internal/nursery/user/entry-preference",
            headers=self.headers,
            json={"preference": "hidden", "account_id": "someone-else"},
        )
        self.assertEqual(unknown.status_code, 422)
        self.assertEqual(unknown.json()["error"]["code"], "UNKNOWN_REQUEST_FIELD")

    def test_valid_runtime_settings_create_only_the_separate_nursery_storage(self):
        with tempfile.TemporaryDirectory() as root:
            db_path = Path(root) / "separate" / "nursery.sqlite3"
            vault_path = Path(root) / "separate" / "nursery-secrets"
            provider = NurseryRuntimeProvider(
                {"buckets_dir": str(Path(root) / "unrelated-memory")},
                environment={
                    INTERNAL_TOKEN_ENV: INTERNAL_TOKEN,
                    DB_PATH_ENV: str(db_path),
                    VAULT_DIR_ENV: str(vault_path),
                    VAULT_KEY_ENV: FernetFileSecretVault.generate_key(),
                    RULES_PATH_ENV: str(RULES_PATH),
                },
            )
            client = TestClient(make_app(NurseryInternalHTTPAPI(provider)))
            try:
                response = client.get(
                    "/internal/nursery/user/status", headers=self.headers
                )
                self.assertEqual(response.status_code, 200, response.text)
                self.assertEqual(
                    response.json()["status"]["module_state"], "never_enabled"
                )
                self.assertTrue(db_path.is_file())
                self.assertTrue(vault_path.is_dir())
                self.assertFalse((Path(root) / "unrelated-memory").exists())
            finally:
                client.close()

    def test_user_interaction_uses_its_own_route_and_cannot_run_in_a_draft(self):
        child_id, _ = self.start_draft()
        response = self.client.post(
            f"/internal/nursery/user/children/{child_id}/interactions",
            headers=self.headers,
            json={
                "message": "今天开不开心呀？",
                "operation_id": str(uuid.uuid4()),
                "idempotency_key": "child-interaction-in-draft",
                "expected_state_version": 0,
            },
        )
        self.assertEqual(response.status_code, 200, response.text)
        operation = response.json()["operation"]
        self.assertEqual(operation["status"], "rejected")
        self.assertEqual(operation["error"]["code"], "INVALID_STATE_TRANSITION")
        self.assertEqual(self.adapter.calls, 0)

    def test_invalid_rules_fail_before_creating_database(self):
        with tempfile.TemporaryDirectory() as root:
            db_path = Path(root) / "nursery.sqlite3"
            provider = NurseryRuntimeProvider(
                {"buckets_dir": root},
                environment={
                    INTERNAL_TOKEN_ENV: INTERNAL_TOKEN,
                    DB_PATH_ENV: str(db_path),
                    VAULT_KEY_ENV: FernetFileSecretVault.generate_key(),
                    RULES_PATH_ENV: str(Path(root) / "missing-rules.yaml"),
                },
            )
            client = TestClient(make_app(NurseryInternalHTTPAPI(provider)))
            try:
                response = client.get(
                    "/internal/nursery/user/status", headers=self.headers
                )
                self.assertEqual(response.status_code, 503)
                self.assertFalse(db_path.exists())
            finally:
                client.close()

    def test_brain_registers_only_private_nursery_routes(self):
        import server

        app = server.mcp.streamable_http_app()
        paths = {
            getattr(route, "path", "")
            for route in app.routes
            if "nursery" in getattr(route, "path", "")
        }
        self.assertEqual(
            paths,
            {
                "/internal/nursery/anima/family-events",
                "/internal/nursery/anima/family-events/supersede",
                "/internal/nursery/anima/family-events/revoke",
                "/internal/nursery/user/status",
                "/internal/nursery/user/entry-preference",
                "/internal/nursery/user/drafts",
                "/internal/nursery/user/drafts/{child_id}",
                "/internal/nursery/user/drafts/{child_id}/operations",
                "/internal/nursery/user/operations/{operation_id}",
                "/internal/nursery/user/children/{child_id}/interactions",
                "/internal/nursery/user/children/{child_id}/status",
                "/internal/nursery/user/children/{child_id}/operations",
            },
        )
        self.assertFalse(any(path.startswith("/api/") for path in paths))

    def test_status_start_and_draft_reads_are_side_effect_free(self):
        before = self.client.get(
            "/internal/nursery/user/status", headers=self.headers
        )
        self.assertEqual(before.json()["status"]["module_state"], "never_enabled")
        child_id, _ = self.start_draft()
        operation_count = self.store.count_rows("nursery_operations")
        for _ in range(5):
            status = self.client.get(
                "/internal/nursery/user/status", headers=self.headers
            ).json()["status"]
            draft = self.client.get(
                f"/internal/nursery/user/drafts/{child_id}", headers=self.headers
            ).json()["draft"]
            self.assertEqual(status["module_state"], "draft")
            self.assertEqual(draft["child_id"], child_id)
        self.assertEqual(
            self.store.count_rows("nursery_operations"), operation_count
        )
        self.assertEqual(self.adapter.calls, 0)

    def test_request_cannot_supply_actor_identity(self):
        child_id, _ = self.start_draft()
        response = self.operation(
            child_id,
            "save_initial_space",
            payload={"space": {"room_overall": "安静"}},
            actor={"role": "external_ai_guardian"},
        )
        self.assertEqual(response.status_code, 422)
        self.assertEqual(response.json()["error"]["code"], "UNKNOWN_REQUEST_FIELD")
        draft = self.client.get(
            f"/internal/nursery/user/drafts/{child_id}", headers=self.headers
        ).json()["draft"]
        self.assertFalse(draft["initial_space_completed"])

    def test_model_secret_never_returns_or_enters_sqlite(self):
        child_id, _ = self.start_draft()
        bound = self.operation(
            child_id,
            "bind_external_guardian",
            payload={"caregiver_id": "external-ai", "display_name": "沈野"},
        )
        self.assertEqual(bound.status_code, 200, bound.text)
        secret = "test-key-http-private-9876"
        saved = self.operation(
            child_id,
            "save_model_connection",
            payload={
                "provider_id": "fake",
                "base_url": "https://models.example/v1",
                "model_name": "child-model",
                "timeout_seconds": 5,
            },
            private={"credential": secret},
        )
        self.assertEqual(saved.status_code, 200, saved.text)
        self.assertNotIn(secret, saved.text)
        operation_id = saved.json()["operation"]["operation_id"]
        replay = self.client.get(
            f"/internal/nursery/user/operations/{operation_id}",
            headers=self.headers,
        )
        self.assertEqual(replay.status_code, 200)
        self.assertNotIn(secret, replay.text)
        self.assertNotIn(secret.encode("utf-8"), self.db_path.read_bytes())
        self.assertEqual(self.adapter.calls, 1)

    def test_phone_and_personal_mcp_complete_one_shared_creation_and_talk(self):
        child_id, _ = self.start_draft()
        mcp = FakeMCP()
        register_external_nursery_mcp_tools(
            mcp,
            tools=ExternalNurseryTools(
                store=self.store,
                creation=self.runtime.creation,
                interaction=self.runtime.interaction,
            ),
            resolver=BoundExternalMCPActorResolver(self.store),
        )

        def user(action, payload=None, private=None):
            response = self.operation(
                child_id,
                action,
                payload=payload or {},
                private=private,
            )
            self.assertEqual(response.status_code, 200, response.text)
            operation = response.json()["operation"]
            self.assertEqual(operation["status"], "completed", operation)
            return operation

        def external(tool_name, **arguments):
            document = json.loads(asyncio.run(mcp.functions[tool_name](**arguments)))
            self.assertTrue(document["ok"], document)
            return document["result"]

        secret = "test-key-end-to-end-private"
        user(
            "save_model_connection",
            {
                "provider_id": "fake",
                "base_url": "https://models.example/v1",
                "model_name": "child-model",
            },
            {"credential": secret},
        )
        user(
            "save_draft_identity",
            {
                "sex_status": "boy",
                "stage_id": "infancy",
                "nickname": "小树",
                "address_terms": {},
            },
        )
        user("save_name_proposals", {"proposals": [{"name": "小树"}]})
        draft = self.client.get(
            f"/internal/nursery/user/drafts/{child_id}", headers=self.headers
        ).json()["draft"]
        candidate_id = draft["name_candidates"][0]["candidate_id"]
        user("review_name_candidates", {"preferences": {candidate_id: "like"}})
        external(
            "nursery_submit_name_opinions",
            opinions=[{"name": "小树", "opinion": "like"}],
        )
        draft = self.client.get(
            f"/internal/nursery/user/drafts/{child_id}", headers=self.headers
        ).json()["draft"]
        self.assertEqual(draft["official_name"], "小树")

        user(
            "submit_temperament_questionnaire",
            private={"answers": {question_id: "middle" for question_id in TEMPERAMENT_QUESTIONS}},
        )
        user(
            "submit_initial_style",
            private={"answers": {question_id: "middle" for question_id in INITIAL_STYLE_QUESTIONS}},
        )
        form = external("nursery_questionnaire_form", kind="all")
        self.assertEqual(set(form["forms"]), {"temperament", "initial_style"})
        external(
            "nursery_submit_temperament_answers",
            answers={question_id: "high" for question_id in TEMPERAMENT_QUESTIONS},
        )
        external(
            "nursery_submit_care_style_answers",
            answers={question_id: "middle" for question_id in INITIAL_STYLE_QUESTIONS},
        )
        user("save_initial_space", {"space": {"room_overall": "安静温暖的小房间"}})

        draft = self.client.get(
            f"/internal/nursery/user/drafts/{child_id}", headers=self.headers
        ).json()["draft"]
        self.assertEqual(draft["missing_requirements"], [])
        subject_version = draft["draft_version"]
        user("confirm_creation", {"subject_version": subject_version})
        activation = external(
            "nursery_creation_submit",
            action="confirm_creation",
            payload={"subject_version": subject_version},
        )
        self.assertEqual(activation["module_state"], "active")

        status = self.client.get(
            "/internal/nursery/user/status", headers=self.headers
        ).json()["status"]
        self.assertEqual(status["module_state"], "active")
        self.assertEqual(status["child_name"], "小树")
        simple = external("child_interact", message="小树，你现在想做什么？")
        self.assertEqual(simple["status"], "completed", simple)
        status = self.client.get(
            "/internal/nursery/user/status", headers=self.headers
        ).json()["status"]
        first_operation = str(uuid.uuid4())
        first = external(
            "child_interact",
            operation_id=first_operation,
            idempotency_key=first_operation,
            expected_state_version=status["state_version"],
            message="小树，今天开心吗？",
        )
        self.assertEqual(first["status"], "completed", first)
        self.assertEqual(first["interaction"]["reply"], "我听见啦，想和你再说一会儿。")
        replay = external(
            "child_interact",
            operation_id=first_operation,
            idempotency_key=first_operation,
            expected_state_version=status["state_version"],
            message="小树，今天开心吗？",
        )
        self.assertTrue(replay["replayed"])
        latest = self.client.get(
            "/internal/nursery/user/status", headers=self.headers
        ).json()["status"]
        phone_reply = self.client.post(
            f"/internal/nursery/user/children/{child_id}/interactions",
            headers=self.headers,
            json={
                "message": "爸爸妈妈都很爱你。",
                "operation_id": str(uuid.uuid4()),
                "idempotency_key": str(uuid.uuid4()),
                "expected_state_version": latest["state_version"],
            },
        )
        self.assertEqual(phone_reply.status_code, 200, phone_reply.text)
        self.assertEqual(phone_reply.json()["operation"]["status"], "completed")
        self.assertNotIn(secret.encode("utf-8"), self.db_path.read_bytes())

        denied = json.loads(
            asyncio.run(
                mcp.functions["nursery_creation_submit"](
                    action="save_model_connection", payload={}
                )
            )
        )
        self.assertFalse(denied["ok"])
        self.assertEqual(denied["error"]["code"], "EXTERNAL_TOOL_ACTION_NOT_AVAILABLE")

    def test_non_creation_action_is_not_exposed(self):
        child_id, _ = self.start_draft()
        response = self.operation(child_id, "pause")
        self.assertEqual(response.status_code, 422)
        self.assertEqual(response.json()["error"]["code"], "ACTION_NOT_EXPOSED")


class NurseryManagerProxyTests(unittest.TestCase):
    def setUp(self):
        manager_server.login_failures.clear()
        self.password = patch.object(
            manager_server, "manager_password", "nursery-manager-password"
        )
        self.auth_path = patch.object(
            manager_server,
            "manager_auth_path",
            Path(tempfile.gettempdir()) / f"missing-{uuid.uuid4()}.json",
        )
        self.environment = patch.dict(
            os.environ,
            {
                "OMBRE_NURSERY_INTERNAL_TOKEN": INTERNAL_TOKEN,
                "OMBRE_BRAIN_INTERNAL_URL": "http://ombre-brain:8000",
            },
        )
        self.password.start()
        self.auth_path.start()
        self.environment.start()
        self.client = TestClient(manager_server.app)

    def tearDown(self):
        self.client.close()
        self.environment.stop()
        self.auth_path.stop()
        self.password.stop()

    def login(self):
        response = self.client.post(
            "/api/auth/login", json={"password": "nursery-manager-password"}
        )
        self.assertEqual(response.status_code, 200)

    def test_manager_requires_login_before_proxying(self):
        factory = patch.object(manager_server, "_new_nursery_proxy_client")
        mocked = factory.start()
        try:
            response = self.client.get("/api/nursery/status")
            self.assertEqual(response.status_code, 401)
            mocked.assert_not_called()
        finally:
            factory.stop()

    def test_manager_forwards_to_brain_without_returning_private_value(self):
        self.login()
        secret = "test-key-manager-private-5555"

        def handler(request: httpx.Request) -> httpx.Response:
            self.assertEqual(
                request.headers.get("x-anima-internal-token"), INTERNAL_TOKEN
            )
            self.assertEqual(
                request.url.path,
                "/internal/nursery/user/drafts/child-1/operations",
            )
            self.assertIn(secret.encode("utf-8"), request.content)
            return httpx.Response(
                200,
                json={
                    "ok": True,
                    "operation": {"operation_id": "op", "status": "completed"},
                },
            )

        def client_factory():
            return httpx.AsyncClient(transport=httpx.MockTransport(handler))

        presence = patch.object(
            manager_server.xinchao_service,
            "observe_presence",
            new=AsyncMock(return_value={"cycle_id": 1}),
        )
        cancel = patch.object(
            manager_server.behavior_service.store,
            "cancel_for_activity",
            new=AsyncMock(),
        )
        factory = patch.object(
            manager_server, "_new_nursery_proxy_client", side_effect=client_factory
        )
        presence.start()
        cancel.start()
        factory.start()
        try:
            response = self.client.post(
                "/api/nursery/drafts/child-1/operations",
                json={
                    "action": "save_model_connection",
                    "operation_id": str(uuid.uuid4()),
                    "idempotency_key": "manager-proxy",
                    "payload": {},
                    "private": {"credential": secret},
                },
            )
            self.assertEqual(response.status_code, 200, response.text)
            self.assertNotIn(secret, response.text)
        finally:
            factory.stop()
            cancel.stop()
            presence.stop()

    def test_manager_proxies_entry_preference_get_and_put(self):
        self.login()
        seen: list[tuple[str, bytes]] = []

        def handler(request: httpx.Request) -> httpx.Response:
            self.assertEqual(
                request.url.path, "/internal/nursery/user/entry-preference"
            )
            self.assertEqual(
                request.headers.get("x-anima-internal-token"), INTERNAL_TOKEN
            )
            seen.append((request.method, request.content))
            value = "hidden" if request.method == "PUT" else "undecided"
            return httpx.Response(200, json={"ok": True, "preference": value})

        def client_factory():
            return httpx.AsyncClient(transport=httpx.MockTransport(handler))

        with patch.object(
            manager_server, "_new_nursery_proxy_client", side_effect=client_factory
        ):
            get_response = self.client.get("/api/nursery/entry-preference")
            put_response = self.client.put(
                "/api/nursery/entry-preference", json={"preference": "hidden"}
            )
        self.assertEqual(get_response.status_code, 200, get_response.text)
        self.assertEqual(put_response.status_code, 200, put_response.text)
        self.assertEqual(seen[0], ("GET", b""))
        self.assertEqual(json.loads(seen[1][1]), {"preference": "hidden"})


if __name__ == "__main__":
    unittest.main()
