from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from nursery.mcp_tools import BoundExternalMCPActorResolver
from nursery.models import (
    ANIMA_MCP_ALLOWED_ACTIONS,
    AuthSource,
    CaregiverRole,
    ModuleState,
    NurseryAction,
    NurseryError,
    ensure_actor_allowed,
)
from nursery.store import NurseryStore, iso_time


class ExistingAnimaMCPResolverTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.store = NurseryStore(Path(self.temp.name) / "nursery.sqlite3")
        self.store.create_fixture(
            account_id="account", child_id="child", state=ModuleState.ACTIVE
        )
        stamp = iso_time(self.store.clock())
        with self.store._write() as connection:  # test fixture only
            connection.execute(
                """
                INSERT INTO nursery_caregivers (
                    caregiver_id, account_id, role, permission_status, created_at, updated_at
                ) VALUES (?, 'account', ?, 'active', ?, ?)
                """,
                ("anima", CaregiverRole.EXTERNAL_AI_GUARDIAN.value, stamp, stamp),
            )
            connection.execute(
                """
                INSERT INTO nursery_caregiver_profiles (
                    caregiver_id, child_id, display_name, founding_guardian, created_at, updated_at
                ) VALUES ('anima', 'child', 'Anima', 1, ?, ?)
                """,
                (stamp, stamp),
            )
        self.resolver = BoundExternalMCPActorResolver(self.store)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_personal_anima_mcp_uses_the_one_module_caregiver(self) -> None:
        actor = self.resolver.resolve_external_actor()
        self.assertIsNotNone(actor)
        assert actor is not None
        self.assertEqual(actor.account_id, "account")
        self.assertEqual(actor.caregiver_id, "anima")
        self.assertEqual(actor.role, CaregiverRole.EXTERNAL_AI_GUARDIAN)
        self.assertEqual(actor.auth_source, AuthSource.MCP_SESSION)
        self.assertEqual(actor.permissions, frozenset({"nursery.external.tools"}))
        for action in ANIMA_MCP_ALLOWED_ACTIONS:
            with self.subTest(action=action.value):
                ensure_actor_allowed(actor, action)
        for action in set(NurseryAction) - ANIMA_MCP_ALLOWED_ACTIONS - {
            NurseryAction.SYNC_ANIMA_CONTEXT,
            NurseryAction.APPLY_ANIMA_FAMILY_EVENT,
        }:
            with self.subTest(action=action.value):
                with self.assertRaises(NurseryError):
                    ensure_actor_allowed(actor, action)

    def test_no_second_guardian_position_means_tools_return_a_boundary_error(self) -> None:
        with self.store._write() as connection:  # test fixture only
            connection.execute("DELETE FROM nursery_caregiver_profiles WHERE caregiver_id='anima'")
        self.assertIsNone(self.resolver.resolve_external_actor())

    def test_existing_nursery_backfills_its_personal_anima_position_once(self) -> None:
        with self.store._write() as connection:  # test fixture only
            connection.execute("DELETE FROM nursery_caregiver_profiles WHERE caregiver_id='anima'")
            connection.execute("DELETE FROM nursery_caregivers WHERE caregiver_id='anima'")
        self.assertTrue(self.store.ensure_personal_mcp_guardian())
        actor = self.resolver.resolve_external_actor()
        self.assertIsNotNone(actor)
        assert actor is not None
        self.assertEqual(actor.caregiver_id, "anima-external-ai-guardian")
        self.assertFalse(self.store.ensure_personal_mcp_guardian())

if __name__ == "__main__":
    unittest.main()
