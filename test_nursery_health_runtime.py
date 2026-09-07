from __future__ import annotations

import asyncio
import tempfile
import unittest
import uuid
from datetime import datetime, timezone
from pathlib import Path

from nursery.health_contract import HealthEvaluationService
from nursery.coordinator import NurseryCoordinator, PreparedMutation
from nursery.health_runtime import NurseryHealthRuntime, evaluate_family_event_health
from nursery.interaction_service import NurseryInteractionService
from nursery.model_connection import AdapterRegistry, ChildConversationProposal
from nursery.models import (
    ActorContext,
    AuthSource,
    CaregiverRole,
    HealthOutcome,
    ModuleState,
    NurseryAction,
    NurseryCommand,
    NurseryError,
    SourceReference,
    SourceType,
)
from nursery.secret_vault import InMemorySecretVault
from nursery.store import NurseryStore, iso_time


class CountingNoChangeRule:
    rule_version = "health-runtime-counting-test-v1"

    def __init__(self) -> None:
        self.calls = 0

    def evaluate(self, _conditions):
        self.calls += 1
        return HealthOutcome.NO_CHANGE


class FakeAdapter:
    def respond(self, _request, conversation):
        return ChildConversationProposal(
            reply="我想和你坐一会儿。",
            intent="talk",
            runtime_state={
                "thirst": conversation.runtime_state["thirst"],
                "hunger": conversation.runtime_state["hunger"],
                "fatigue": conversation.runtime_state["fatigue"],
                "comfort": 0.8,
                "connection": 0.7,
                "play_drive": 0.5,
                "unwell": conversation.runtime_state["unwell"],
            },
        )


class BrokenAdapter:
    def respond(self, _request, _conversation):
        raise RuntimeError("model unavailable")


class NurseryHealthRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.now = datetime(2026, 9, 3, tzinfo=timezone.utc)
        self.store = NurseryStore(
            Path(self.temp.name) / "nursery.sqlite3", clock=lambda: self.now
        )
        self.store.create_fixture(
            account_id="account", child_id="child", state=ModuleState.ACTIVE
        )
        self.actor = ActorContext.build(
            account_id="account",
            caregiver_id="guardian",
            role=CaregiverRole.USER_GUARDIAN,
            permissions=("nursery.*",),
            auth_source=AuthSource.MANAGER_SESSION,
        )
        stamp = iso_time(self.now)
        with self.store._write() as connection:  # isolated fixture only
            connection.execute(
                """
                INSERT INTO nursery_caregivers (
                    caregiver_id, account_id, role, permission_status, created_at, updated_at
                ) VALUES (?, ?, 'user_guardian', 'active', ?, ?)
                """,
                (self.actor.caregiver_id, self.actor.account_id, stamp, stamp),
            )
            connection.execute(
                """
                INSERT INTO nursery_model_configs (
                    account_id, provider_id, base_url, model_name, credential_ref,
                    credential_suffix, connection_status, capabilities_json,
                    config_version, tested_at, created_at, updated_at
                ) VALUES (?, 'fake', 'https://models.example/v1', 'child-model',
                    'model:account', 'cret', 'ready', '{}', 1, ?, ?, ?)
                """,
                (self.actor.account_id, stamp, stamp, stamp),
            )
        self.vault = InMemorySecretVault()
        self.vault.put("model:account", "test-secret")

    def tearDown(self):
        self.temp.cleanup()

    def service(self, adapter, rule=None):
        registry = AdapterRegistry()
        registry.register("fake", adapter)
        runtime = None
        if rule is not None:
            runtime = NurseryHealthRuntime(
                self.store, evaluator=HealthEvaluationService(self.store, rule)
            )
        return NurseryInteractionService(
            self.store,
            vault=self.vault,
            adapter_registry=registry,
            health_runtime=runtime,
        )

    @staticmethod
    def interaction_source(operation_id: str) -> SourceReference:
        return SourceReference.build(
            source_type=SourceType.USER_INTERACTION,
            source_id=f"test:interaction:{operation_id}",
            source_version="v1",
        )

    def test_committed_interaction_evaluates_once_and_replay_does_not_rerun_rule(self):
        rule = CountingNoChangeRule()
        service = self.service(FakeAdapter(), rule)
        operation_id = str(uuid.uuid4())
        source = self.interaction_source(operation_id)
        before_runtime = self.store.get_child_runtime_state("child")
        kwargs = {
            "actor": self.actor,
            "child_id": "child",
            "operation_id": operation_id,
            "idempotency_key": "health-runtime-interaction",
            "expected_state_version": 0,
            "message": "今天想和你聊聊天。",
            "source": source,
        }
        first = asyncio.run(service.interact(**kwargs))
        replay = asyncio.run(service.interact(**kwargs))

        self.assertEqual(first["status"], "completed", first)
        self.assertTrue(replay["replayed"], replay)
        self.assertEqual(rule.calls, 1)
        self.assertEqual(self.store.count_rows("nursery_health_evaluations"), 1)
        with self.store._read() as connection:
            row = connection.execute(
                "SELECT outcome, trigger_type, operation_id FROM nursery_health_evaluations"
            ).fetchone()
        self.assertEqual(tuple(row), ("no_change", "interaction", operation_id))
        after_runtime = self.store.get_child_runtime_state("child")
        for field in ("thirst", "hunger", "fatigue", "unwell"):
            self.assertEqual(after_runtime[field], before_runtime[field])
        self.assertEqual(self.store.get_state("account", "child")["state_version"], 1)

    def test_failed_interaction_does_not_evaluate_health(self):
        rule = CountingNoChangeRule()
        service = self.service(BrokenAdapter(), rule)
        operation_id = str(uuid.uuid4())
        result = asyncio.run(
            service.interact(
                actor=self.actor,
                child_id="child",
                operation_id=operation_id,
                idempotency_key="health-runtime-failed-interaction",
                expected_state_version=0,
                message="今天还好吗？",
                source=self.interaction_source(operation_id),
            )
        )

        self.assertEqual(result["status"], "failed_retryable", result)
        self.assertEqual(rule.calls, 0)
        self.assertEqual(self.store.count_rows("nursery_health_evaluations"), 0)

    def test_reads_and_uncommitted_sources_do_not_evaluate_health(self):
        runtime = NurseryHealthRuntime(self.store)
        source = self.interaction_source("never-committed")
        state = self.store.get_state("account", "child")
        self.store.get_child_runtime_state("child")
        self.store.get_body_clock("child")

        with self.assertRaises(NurseryError) as caught:
            runtime.evaluate_interaction(
                account_id="account",
                child_id="child",
                source=source,
                operation_id="never-committed",
            )

        self.assertEqual(caught.exception.code, "HEALTH_COMMITTED_SOURCE_REQUIRED")
        self.assertEqual(self.store.count_rows("nursery_health_evaluations"), 0)
        self.assertEqual(self.store.get_state("account", "child"), state)

    def _commit_mailbox_context(self, source: SourceReference) -> str:
        operation_id = str(uuid.uuid4())
        actor = ActorContext.build(
            account_id="account",
            caregiver_id="anima-memory-manager:account",
            role=CaregiverRole.SYSTEM_EVENT,
            permissions=("nursery.anima.context.sync",),
            auth_source=AuthSource.INTERNAL_EVENT,
        )

        def prepare(_command, _before):
            return PreparedMutation(
                data={
                    "anima_context": {
                        "source_key": source.source_id,
                        "source_version": source.source_version,
                        "category": "care",
                        "summary": "家里刚完成了一次温和的照料。",
                        "occurred_at": iso_time(self.now),
                        "disposition": {},
                    }
                }
            )

        result = asyncio.run(
            NurseryCoordinator(self.store, prepare_hook=prepare).submit(
                NurseryCommand(
                    actor=actor,
                    child_id="child",
                    action=NurseryAction.SYNC_ANIMA_CONTEXT,
                    operation_id=operation_id,
                    idempotency_key=f"mailbox-context-{operation_id}",
                    expected_state_version=0,
                    payload={},
                    source=source,
                )
            )
        )
        self.assertEqual(result.status.value, "completed", result)
        return operation_id

    def test_relevant_committed_mailbox_event_records_one_default_no_change_evaluation(self):
        source = SourceReference.build(
            source_type=SourceType.MAILBOX_EVENT,
            source_id="mailbox:family-event-1",
            source_version="v1",
        )
        operation_id = self._commit_mailbox_context(source)
        self.assertIsNone(
            evaluate_family_event_health(
                store=self.store,
                account_id="account",
                child_id="child",
                source=source,
                operation_id=operation_id,
                health_relevant=False,
            )
        )
        evaluated = evaluate_family_event_health(
            store=self.store,
            account_id="account",
            child_id="child",
            source=source,
            operation_id=operation_id,
            health_relevant=True,
        )

        self.assertEqual(evaluated["outcome"], "no_change")
        self.assertEqual(evaluated["trigger_type"], "relevant_mailbox_event")
        self.assertEqual(self.store.count_rows("nursery_health_evaluations"), 1)
        replay = evaluate_family_event_health(
            store=self.store,
            account_id="account",
            child_id="child",
            source=source,
            operation_id=operation_id,
            health_relevant=True,
        )
        self.assertTrue(replay["replayed"])
        self.assertEqual(self.store.count_rows("nursery_health_evaluations"), 1)


if __name__ == "__main__":
    unittest.main()
