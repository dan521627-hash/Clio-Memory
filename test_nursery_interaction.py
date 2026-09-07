from __future__ import annotations

import asyncio
import tempfile
import unittest
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

from nursery.interaction_service import NurseryInteractionService
from nursery.body_state import BodyTimeRule
from nursery.anima_bridge import AnimaNurseryBridge
from nursery.model_connection import (
    AdapterRegistry,
    ChildConversationProposal,
)
from nursery.models import ActorContext, AuthSource, CaregiverRole, ModuleState, SourceReference, SourceType
from nursery.secret_vault import InMemorySecretVault
from nursery.store import DEFAULT_CHILD_RUNTIME_STATE, NurseryStore, iso_time


class FakeChildAdapter:
    def __init__(self) -> None:
        self.last_conversation = None

    def respond(self, _request, conversation):
        self.last_conversation = conversation
        return ChildConversationProposal(
            reply="我有一点想喝温温的水。",
            intent="drink",
            runtime_state={
                "thirst": 0.4,
                "hunger": 0.2,
                "fatigue": 0.2,
                "comfort": 0.8,
                "connection": 0.7,
                "play_drive": 0.5,
                "unwell": 0.0,
            },
        )


class NurseryInteractionTests(unittest.TestCase):
    def test_clear_completed_care_fallback_excludes_questions(self):
        self.assertIsNone(
            NurseryInteractionService._clear_completed_care_from_message("要不要喝点水？", "妈妈")
        )
        care = NurseryInteractionService._clear_completed_care_from_message(
            "妈妈刚刚轻轻抱了抱你，还拍了拍你的背。", "妈妈"
        )
        self.assertEqual(care["kind"], "comfort")
        self.assertEqual(care["status"], "completed")

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.now = datetime(2026, 9, 2, tzinfo=timezone.utc)
        self.store = NurseryStore(
            Path(self.temp.name) / "nursery.sqlite3", clock=lambda: self.now
        )
        self.store.create_fixture(
            account_id="account", child_id="child", state=ModuleState.ACTIVE
        )
        self.actor = ActorContext.build(
            account_id="account",
            caregiver_id="user-guardian",
            role=CaregiverRole.USER_GUARDIAN,
            permissions=("nursery.*",),
            auth_source=AuthSource.MANAGER_SESSION,
        )
        with self.store._write() as connection:  # test fixture only
            stamp = iso_time(self.now)
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
        self.adapter = FakeChildAdapter()
        registry = AdapterRegistry()
        registry.register("fake", self.adapter)
        self.service = NurseryInteractionService(
            self.store, vault=self.vault, adapter_registry=registry
        )

    def tearDown(self):
        self.temp.cleanup()

    def test_interaction_uses_state_and_hard_deletes_after_thirty_days(self):
        message = "宝宝想不想喝点什么？"
        bridge = AnimaNurseryBridge(self.store)
        asyncio.run(
            bridge.accept(
                child_id="child",
                child_safe_event={
                    "source_key": "mailbox:family-care",
                    "source_version": "1",
                    "category": "care",
                    "child_safe_summary": "家里刚完成了一次温和的照料。",
                    "occurred_at": "2026-09-02T00:00:00+00:00",
                },
                disposition_report={"tendencies": []},
            )
        )
        first_context_version = self.store.get_state("account", "child")["state_version"]
        asyncio.run(
            bridge.accept(
                child_id="child",
                child_safe_event={
                    "source_key": "mailbox:family-care",
                    "source_version": "1",
                    "category": "care",
                    "child_safe_summary": "家里刚完成了一次温和的照料。",
                    "occurred_at": "2026-09-02T00:00:00+00:00",
                },
                disposition_report={"tendencies": []},
            )
        )
        self.assertEqual(self.store.get_state("account", "child")["state_version"], first_context_version)
        self.assertEqual(len(self.store.list_active_child_safe_anima_contexts("child")), 1)
        result = asyncio.run(
            self.service.interact(
                actor=self.actor,
                child_id="child",
                operation_id=str(uuid.uuid4()),
                idempotency_key="first-drink-check",
                expected_state_version=int(
                    self.store.get_state("account", "child")["state_version"]
                ),
                message=message,
                source=SourceReference.build(
                    source_type=SourceType.USER_INTERACTION,
                    source_id="test-interaction-1",
                    source_version="v1",
                ),
            )
        )
        self.assertEqual(result.get("error"), None, result)
        self.assertEqual(result["interaction"]["reply"], "我有一点想喝温温的水。")
        self.assertEqual(self.adapter.last_conversation.runtime_state["thirst"], 0.2)
        self.assertEqual(
            self.adapter.last_conversation.anima_contexts[0]["summary"],
            "家里刚完成了一次温和的照料。",
        )
        # A response model may express feelings but cannot author body needs.
        self.assertEqual(self.store.get_child_runtime_state("child")["thirst"], 0.2)
        operation = self.store.get_operation(result["operation_id"])
        self.assertNotIn(message, operation["payload"])

        self.now += timedelta(days=31)
        purged = self.store.purge_expired_child_short_events()
        self.assertEqual(purged["deleted_events"], 1)
        self.assertEqual(purged["deleted_anima_contexts"], 1)
        self.assertEqual(purged["reset_states"], 1)
        self.assertIsNone(self.store.get_short_event_by_operation(result["operation_id"]))
        runtime = self.store.get_child_runtime_state("child")
        self.assertEqual(runtime["thirst"], 0.2)
        self.assertEqual(runtime["hunger"], 0.2)
        self.assertEqual(runtime["fatigue"], 0.2)
        self.assertEqual(runtime["unwell"], 0.0)

    def test_body_settlement_commits_with_successful_interaction(self):
        rule = BodyTimeRule(
            version="body-time-interaction-test-v1",
            per_hour={"hunger": 0.1, "thirst": 0.1, "fatigue": 0.1},
            maximum={"hunger": 1.0, "thirst": 1.0, "fatigue": 1.0},
        )
        service = NurseryInteractionService(
            self.store,
            vault=self.vault,
            adapter_registry=self.service.adapter_registry,
            body_time_rule=rule,
        )
        with self.store._write() as connection:  # isolated fixture only
            stamp = iso_time(self.now)
            connection.execute(
                """
                INSERT INTO nursery_body_clocks (
                    child_id, settled_through, rule_version, updated_at
                ) VALUES ('child', ?, ?, ?)
                """,
                (stamp, rule.version, stamp),
            )
        self.now += timedelta(hours=1)
        result = asyncio.run(
            service.interact(
                actor=self.actor,
                child_id="child",
                operation_id=str(uuid.uuid4()),
                idempotency_key="body-time-success",
                expected_state_version=0,
                message="你现在感觉怎么样？",
                source=SourceReference.build(
                    source_type=SourceType.USER_INTERACTION,
                    source_id="body-time-success",
                    source_version="v1",
                ),
            )
        )
        self.assertEqual(result["status"], "completed", result)
        self.assertEqual(self.adapter.last_conversation.runtime_state["hunger"], 0.3)
        self.assertEqual(
            self.store.get_body_clock("child")["settled_through"], iso_time(self.now)
        )

    def test_model_failure_does_not_consume_body_time(self):
        class BrokenAdapter(FakeChildAdapter):
            def respond(self, _request, conversation):
                self.last_conversation = conversation
                raise RuntimeError("model unavailable")

        broken = BrokenAdapter()
        registry = AdapterRegistry()
        registry.register("fake", broken)
        rule = BodyTimeRule(
            version="body-time-interaction-test-v1",
            per_hour={"hunger": 0.1, "thirst": 0.1, "fatigue": 0.1},
            maximum={"hunger": 1.0, "thirst": 1.0, "fatigue": 1.0},
        )
        service = NurseryInteractionService(
            self.store,
            vault=self.vault,
            adapter_registry=registry,
            body_time_rule=rule,
        )
        with self.store._write() as connection:  # isolated fixture only
            stamp = iso_time(self.now)
            connection.execute(
                """
                INSERT INTO nursery_body_clocks (
                    child_id, settled_through, rule_version, updated_at
                ) VALUES ('child', ?, ?, ?)
                """,
                (stamp, rule.version, stamp),
            )
        before_state = self.store.get_child_runtime_state("child")
        before_clock = self.store.get_body_clock("child")
        self.now += timedelta(hours=1)
        result = asyncio.run(
            service.interact(
                actor=self.actor,
                child_id="child",
                operation_id=str(uuid.uuid4()),
                idempotency_key="body-time-model-failure",
                expected_state_version=0,
                message="你现在感觉怎么样？",
                source=SourceReference.build(
                    source_type=SourceType.USER_INTERACTION,
                    source_id="body-time-model-failure",
                    source_version="v1",
                ),
            )
        )
        self.assertEqual(result["status"], "failed_retryable")
        self.assertEqual(broken.last_conversation.runtime_state["hunger"], 0.3)
        self.assertEqual(self.store.get_child_runtime_state("child"), before_state)
        self.assertEqual(self.store.get_body_clock("child"), before_clock)

    def test_invalid_background_proposal_keeps_reply_without_second_chat_call(self):
        class RepairingAdapter(FakeChildAdapter):
            def __init__(adapter_self):
                super().__init__()
                adapter_self.calls = []

            def respond(adapter_self, _request, conversation):
                adapter_self.calls.append(conversation)
                if len(adapter_self.calls) == 1:
                    return ChildConversationProposal(
                        reply="我听见啦。",
                        intent="talk",
                        runtime_state={"comfort": 9},
                    )
                return super(RepairingAdapter, adapter_self).respond(_request, conversation)

        adapter = RepairingAdapter()
        registry = AdapterRegistry()
        registry.register("fake", adapter)
        service = NurseryInteractionService(self.store, vault=self.vault, adapter_registry=registry)
        result = asyncio.run(
            service.interact(
                actor=self.actor,
                child_id="child",
                operation_id=str(uuid.uuid4()),
                idempotency_key="repair-invalid-proposal",
                expected_state_version=0,
                message="你现在好吗？",
                source=SourceReference.build(
                    source_type=SourceType.USER_INTERACTION,
                    source_id="repair-invalid-proposal",
                    source_version="v1",
                ),
            )
        )
        self.assertEqual(result["status"], "completed", result)
        self.assertEqual(len(adapter.calls), 1)
        self.assertEqual(result["interaction"]["reply"], "我听见啦。")
        self.assertEqual(self.store.get_child_runtime_state("child")["comfort"], 0.7)

    def test_invalid_optional_care_does_not_swallow_child_reply(self):
        class MalformedCareAdapter(FakeChildAdapter):
            def respond(adapter_self, _request, conversation):
                proposal = super(MalformedCareAdapter, adapter_self).respond(_request, conversation)
                return ChildConversationProposal(
                    reply=proposal.reply,
                    intent=proposal.intent,
                    runtime_state=proposal.runtime_state,
                    care_action={"kind": "drink", "status": "completed"},
                )

        adapter = MalformedCareAdapter()
        registry = AdapterRegistry()
        registry.register("fake", adapter)
        service = NurseryInteractionService(self.store, vault=self.vault, adapter_registry=registry)
        result = asyncio.run(
            service.interact(
                actor=self.actor,
                child_id="child",
                operation_id=str(uuid.uuid4()),
                idempotency_key="malformed-care-keeps-reply",
                expected_state_version=0,
                message="给你水喝吧。",
                source=SourceReference.build(
                    source_type=SourceType.USER_INTERACTION,
                    source_id="malformed-care-keeps-reply",
                    source_version="v1",
                ),
            )
        )
        self.assertEqual(result["status"], "completed", result)
        self.assertEqual(result["interaction"]["reply"], "我有一点想喝温温的水。")
        self.assertIsNone(result["interaction"]["care"])

    def test_second_invalid_state_keeps_reply_without_writing_model_state(self):
        class ReplyWithBadStateAdapter(FakeChildAdapter):
            def respond(adapter_self, _request, conversation):
                return ChildConversationProposal(
                    reply="好呀，我听见你啦。",
                    intent="drink",
                    runtime_state={"comfort": 9},
                    care_action={"kind": "drink", "status": "completed"},
                )

        before = self.store.get_child_runtime_state("child")
        registry = AdapterRegistry()
        registry.register("fake", ReplyWithBadStateAdapter())
        service = NurseryInteractionService(self.store, vault=self.vault, adapter_registry=registry)
        result = asyncio.run(
            service.interact(
                actor=self.actor,
                child_id="child",
                operation_id=str(uuid.uuid4()),
                idempotency_key="reply-only-after-invalid-state",
                expected_state_version=0,
                message="给你水喝吧。",
                source=SourceReference.build(
                    source_type=SourceType.USER_INTERACTION,
                    source_id="reply-only-after-invalid-state",
                    source_version="v1",
                ),
            )
        )
        self.assertEqual(result["status"], "completed", result)
        self.assertEqual(result["interaction"]["reply"], "好呀，我听见你啦。")
        self.assertIsNone(result["interaction"]["care"])
        self.assertEqual(self.store.get_child_runtime_state("child"), before)

    def test_common_english_emotion_labels_are_saved_in_chinese(self):
        previous = dict(DEFAULT_CHILD_RUNTIME_STATE)
        result = self.service._emotional_state(
            previous,
            {
                "primary": "happy",
                "secondary": ["curious", "safe"],
                "valence": 0.7,
                "arousal": 0.4,
                "safety": 0.9,
                "cause": "听见了熟悉的声音",
            },
        )
        self.assertEqual(result["primary"], "开心")
        self.assertEqual(result["secondary"], ["好奇", "安心"])

    def test_model_cannot_modify_saved_unwell_value(self):
        class ClearingAdapter(FakeChildAdapter):
            def respond(self, _request, conversation):
                self.last_conversation = conversation
                return ChildConversationProposal(
                    reply="我想先安静地靠一会儿。",
                    intent="rest",
                    runtime_state={
                        name: conversation.runtime_state[name]
                        for name in (
                            "thirst",
                            "hunger",
                            "fatigue",
                            "comfort",
                            "connection",
                            "play_drive",
                        )
                    }
                    | {"unwell": 0.2},
                )

        clearing = ClearingAdapter()
        registry = AdapterRegistry()
        registry.register("fake", clearing)
        service = NurseryInteractionService(
            self.store, vault=self.vault, adapter_registry=registry
        )
        with self.store._write() as connection:  # isolated fixture only
            runtime = dict(DEFAULT_CHILD_RUNTIME_STATE)
            runtime["unwell"] = 0.4
            connection.execute(
                "INSERT INTO nursery_child_runtime_state VALUES (?, ?, ?)",
                ("child", __import__("json").dumps(runtime), iso_time(self.now)),
            )
        result = asyncio.run(
            service.interact(
                actor=self.actor,
                child_id="child",
                operation_id=str(uuid.uuid4()),
                idempotency_key="unwell-model-guard",
                expected_state_version=0,
                message="要不要休息一下？",
                source=SourceReference.build(
                    source_type=SourceType.USER_INTERACTION,
                    source_id="unwell-model-guard",
                    source_version="v1",
                ),
            )
        )
        self.assertEqual(result["status"], "completed", result)
        self.assertEqual(self.store.get_child_runtime_state("child")["unwell"], 0.4)

    def test_commit_rechecks_body_cursor_and_rolls_back_interaction(self):
        class RacingAdapter(FakeChildAdapter):
            def respond(adapter_self, request, conversation):
                proposal = super(RacingAdapter, adapter_self).respond(request, conversation)
                clock = self.store.get_body_clock("child")
                with self.store._write() as connection:  # isolated competing write
                    connection.execute(
                        """
                        INSERT INTO nursery_body_clocks (
                            child_id, settled_through, rule_version, updated_at
                        ) VALUES ('child', ?, 'competing-v1', ?)
                        ON CONFLICT(child_id) DO UPDATE SET
                            settled_through=excluded.settled_through,
                            rule_version=excluded.rule_version,
                            updated_at=excluded.updated_at
                        """,
                        (
                            iso_time(self.now + timedelta(seconds=1)),
                            clock["updated_at"],
                        ),
                    )
                return proposal

        registry = AdapterRegistry()
        registry.register("fake", RacingAdapter())
        service = NurseryInteractionService(
            self.store, vault=self.vault, adapter_registry=registry
        )
        before_state = self.store.get_child_runtime_state("child")
        result = asyncio.run(
            service.interact(
                actor=self.actor,
                child_id="child",
                operation_id=str(uuid.uuid4()),
                idempotency_key="body-cursor-race",
                expected_state_version=0,
                message="一起坐一会儿？",
                source=SourceReference.build(
                    source_type=SourceType.USER_INTERACTION,
                    source_id="body-cursor-race",
                    source_version="v1",
                ),
            )
        )
        self.assertEqual(result["status"], "rejected", result)
        self.assertEqual(result["error"]["code"], "BODY_STATE_CHANGED")
        self.assertEqual(self.store.get_child_runtime_state("child"), before_state)
        self.assertEqual(self.store.count_rows("nursery_child_short_events"), 0)


if __name__ == "__main__":
    unittest.main()
