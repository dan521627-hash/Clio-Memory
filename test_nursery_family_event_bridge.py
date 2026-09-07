from __future__ import annotations

import asyncio
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from nursery.anima_bridge import AnimaNurseryBridge
from nursery.health_contract import HealthEvaluationService
from nursery.health_runtime import NurseryHealthRuntime
from nursery.model_connection import (
    AdapterRegistry,
    ChildFamilyEventProposal,
)
from nursery.models import HealthOutcome, ModuleState, NurseryError, SourceType
from nursery.secret_vault import InMemorySecretVault
from nursery.store import NurseryStore, iso_time


class _NoChangeRule:
    rule_version = "family-event-test-v1"

    def __init__(self) -> None:
        self.calls = 0

    def evaluate(self, _conditions):
        self.calls += 1
        return HealthOutcome.NO_CHANGE


class _FamilyAdapter:
    def __init__(self) -> None:
        self.calls = 0

    def respond_to_family_event(self, _request, event):
        self.calls += 1
        return ChildFamilyEventProposal(
            child_reaction="我知道家里刚刚有一点变化，我想靠近你。",
            current_ripple="想再安静地靠一会儿。",
        )


class _BrokenFamilyAdapter(_FamilyAdapter):
    def respond_to_family_event(self, _request, _event):
        self.calls += 1
        raise RuntimeError("model unavailable")


class FamilyEventBridgeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.now = datetime(2026, 9, 3, tzinfo=timezone.utc)
        self.store = NurseryStore(Path(self.temp.name) / "nursery.sqlite3", clock=lambda: self.now)
        self.store.create_fixture(account_id="account", child_id="child", state=ModuleState.ACTIVE)
        stamp = iso_time(self.now)
        with self.store._write() as connection:  # isolated fixture only
            connection.execute(
                """
                INSERT INTO nursery_model_configs (
                    account_id, provider_id, base_url, model_name, credential_ref,
                    credential_suffix, connection_status, capabilities_json,
                    config_version, tested_at, created_at, updated_at
                ) VALUES ('account', 'fake', 'https://models.example/v1', 'child-model',
                    'model:account', 'cret', 'ready', '{}', 1, ?, ?, ?)
                """,
                (stamp, stamp, stamp),
            )
        self.vault = InMemorySecretVault()
        self.vault.put("model:account", "test-secret")
        self.rule = _NoChangeRule()
        self.adapter = _FamilyAdapter()
        registry = AdapterRegistry()
        registry.register("fake", self.adapter)
        self.bridge = AnimaNurseryBridge(
            self.store,
            vault=self.vault,
            adapter_registry=registry,
            health_runtime=NurseryHealthRuntime(
                self.store, evaluator=HealthEvaluationService(self.store, self.rule)
            ),
        )

    def tearDown(self) -> None:
        self.temp.cleanup()

    @staticmethod
    def _event(*, version: str = "v1", health_relevant: bool = False) -> dict:
        return {
            "source_key": "mailbox:42",
            "source_version": version,
            "category": "relationship",
            "child_safe_summary": "家里刚刚用温和的方式把一件事说开了。",
            "occurred_at": "2026-09-03T00:00:00+00:00",
            "health_relevant": health_relevant,
        }

    def test_applies_once_with_experience_ripple_and_optional_health(self):
        before_runtime = self.store.get_child_runtime_state("child")
        first = asyncio.run(
            self.bridge.apply_family_event(
                child_id="child",
                child_safe_event=self._event(health_relevant=True),
                disposition_report={"tendencies": []},
            )
        )
        replay = asyncio.run(
            self.bridge.apply_family_event(
                child_id="child",
                child_safe_event=self._event(health_relevant=True),
                disposition_report={"tendencies": []},
            )
        )
        self.assertTrue(first["family_event_applied"])
        self.assertTrue(replay["family_event_applied"])
        self.assertEqual(self.adapter.calls, 1)
        self.assertEqual(self.rule.calls, 1)
        self.assertEqual(self.store.count_rows("nursery_experiences"), 1)
        view = self.store.get_child_feature_view(
            actor=self._internal_reader(), child_id="child", detail="events"
        )
        event = view["event_view"]["timeline"][0]
        self.assertEqual(event["source_event_id"], "mailbox:42")
        self.assertEqual(event["current_ripple"], "想再安静地靠一会儿。")
        self.assertEqual(event["source_validity"], "valid")
        with self.store._read() as connection:
            source = connection.execute(
                "SELECT operation_id FROM nursery_source_events "
                "WHERE source_id='mailbox:42' AND source_version='v1'"
            ).fetchone()
        snapshots = self.store.snapshots_for(str(source["operation_id"]))
        self.assertEqual([item["snapshot_kind"] for item in snapshots], ["before", "after"])
        self.assertEqual(snapshots[0]["state"]["runtime_state"], before_runtime)
        self.assertEqual(snapshots[1]["state"]["runtime_state"], before_runtime)
        summary = self.store.get_child_feature_view(
            actor=self._internal_reader(), child_id="child", detail="summary"
        )
        self.assertEqual(summary["current_state"]["emotion"]["primary"], "在意")
        self.assertEqual(
            summary["current_state"]["recent_family_event"]["experience_id"],
            "family:" + str(source["operation_id"]),
        )

    def test_failure_leaves_no_family_context_or_experience(self):
        registry = AdapterRegistry()
        broken = _BrokenFamilyAdapter()
        registry.register("fake", broken)
        bridge = AnimaNurseryBridge(self.store, vault=self.vault, adapter_registry=registry)
        with self.assertRaises(NurseryError):
            asyncio.run(
                bridge.apply_family_event(
                    child_id="child", child_safe_event=self._event(), disposition_report={}
                )
            )
        self.assertEqual(broken.calls, 1)
        self.assertEqual(self.store.count_rows("nursery_experiences"), 0)
        self.assertEqual(self.store.list_active_child_safe_anima_contexts("child"), [])

    def test_inactive_child_is_ignored_before_model_or_operation(self):
        with self.store._write() as connection:
            connection.execute("UPDATE nursery_modules SET module_state='paused' WHERE child_id='child'")
        result = asyncio.run(
            self.bridge.apply_family_event(
                child_id="child", child_safe_event=self._event(), disposition_report={}
            )
        )
        self.assertEqual(result, {"family_event_ignored": "nursery_not_active"})
        self.assertEqual(self.adapter.calls, 0)
        self.assertEqual(self.store.count_rows("nursery_operations"), 0)

    def test_supersede_and_revoke_correct_old_experience_and_context(self):
        asyncio.run(
            self.bridge.apply_family_event(
                child_id="child", child_safe_event=self._event(), disposition_report={}
            )
        )
        self.store.supersede_source(
            account_id="account",
            child_id="child",
            source_type=SourceType.MAILBOX_EVENT.value,
            source_id="mailbox:42",
            source_version="v1",
            superseded_by_source_version="v2",
        )
        self.assertEqual(self.store.list_active_child_safe_anima_contexts("child"), [])
        asyncio.run(
            self.bridge.apply_family_event(
                child_id="child", child_safe_event=self._event(version="v2"), disposition_report={}
            )
        )
        self.store.mark_source_revoked(
            account_id="account",
            child_id="child",
            source_type=SourceType.MAILBOX_EVENT.value,
            source_id="mailbox:42",
            source_version="v2",
        )
        self.assertEqual(self.store.list_active_child_safe_anima_contexts("child"), [])
        with self.store._read() as connection:
            rows = connection.execute(
                "SELECT resolution FROM nursery_experiences ORDER BY experience_id"
            ).fetchall()
            ripples = connection.execute(
                "SELECT active FROM nursery_experience_ripples ORDER BY experience_id"
            ).fetchall()
        self.assertEqual([row["resolution"] for row in rows], ["corrected", "corrected"])
        self.assertEqual([row["active"] for row in ripples], [0, 0])

    def _internal_reader(self):
        from nursery.models import ActorContext, AuthSource, CaregiverRole

        actor = ActorContext.build(
            account_id="account",
            caregiver_id="reader",
            role=CaregiverRole.USER_GUARDIAN,
            permissions=("nursery.*",),
            auth_source=AuthSource.MANAGER_SESSION,
        )
        stamp = iso_time(self.now)
        with self.store._write() as connection:
            connection.execute(
                """
                INSERT OR IGNORE INTO nursery_caregivers (
                    caregiver_id, account_id, role, permission_status, created_at, updated_at
                ) VALUES ('reader', 'account', 'user_guardian', 'active', ?, ?)
                """,
                (stamp, stamp),
            )
        return actor


if __name__ == "__main__":
    unittest.main()
