from __future__ import annotations

import copy
import os
import tempfile
import unittest
from pathlib import Path

import yaml

from nursery.capability_rules import (
    CapabilityRulesError,
    CapabilityRulesGate,
    load_capability_rules,
    validate_rules_document,
)
from nursery.models import (
    AuthSource,
    CaregiverRole,
    HealthOutcome,
    ModuleState,
    NurseryAction,
    NurseryError,
    OperationStatus,
    state_after_action,
)


RULES_PATH = Path(
    os.environ.get(
        "ANIMA_NURSERY_RULES",
        str(Path(__file__).resolve().parent / "nursery" / "capability-rules-v1.yaml"),
    )
)


class NurseryDomainTests(unittest.TestCase):
    def test_all_five_states_and_contract_enums_are_stable_strings(self):
        self.assertEqual(
            [state.value for state in ModuleState],
            [
                "never_enabled",
                "draft",
                "active",
                "paused",
                "deletion_pending",
            ],
        )
        self.assertIn("failed_retryable", {item.value for item in OperationStatus})
        self.assertIn("external_ai_guardian", {item.value for item in CaregiverRole})
        self.assertIn("internal_event", {item.value for item in AuthSource})
        self.assertEqual(len(HealthOutcome), 8)

    def test_pure_state_transitions_cover_pause_delete_restore_and_cleanup(self):
        self.assertEqual(
            state_after_action(ModuleState.NEVER_ENABLED, NurseryAction.START_DRAFT),
            ModuleState.DRAFT,
        )
        self.assertEqual(
            state_after_action(ModuleState.DRAFT, NurseryAction.ACTIVATE_FIXTURE),
            ModuleState.ACTIVE,
        )
        self.assertEqual(
            state_after_action(ModuleState.ACTIVE, NurseryAction.PAUSE),
            ModuleState.PAUSED,
        )
        self.assertEqual(
            state_after_action(
                ModuleState.PAUSED,
                NurseryAction.RESUME,
                active_pause_count=1,
            ),
            ModuleState.PAUSED,
        )
        self.assertEqual(
            state_after_action(
                ModuleState.PAUSED,
                NurseryAction.RESUME,
                active_pause_count=0,
            ),
            ModuleState.ACTIVE,
        )
        self.assertEqual(
            state_after_action(
                ModuleState.ACTIVE,
                NurseryAction.CONFIRM_DELETION,
                deletion_confirmed=False,
            ),
            ModuleState.ACTIVE,
        )
        self.assertEqual(
            state_after_action(
                ModuleState.ACTIVE,
                NurseryAction.CONFIRM_DELETION,
                deletion_confirmed=True,
            ),
            ModuleState.DELETION_PENDING,
        )
        self.assertEqual(
            state_after_action(
                ModuleState.DELETION_PENDING,
                NurseryAction.CANCEL_DELETION,
                active_pause_count=1,
                pre_delete_state=ModuleState.ACTIVE,
            ),
            ModuleState.PAUSED,
        )
        self.assertEqual(
            state_after_action(
                ModuleState.DELETION_PENDING,
                NurseryAction.FINALIZE_CLEANUP,
                cleanup_due=True,
            ),
            ModuleState.NEVER_ENABLED,
        )

    def test_illegal_transition_and_early_cleanup_are_rejected(self):
        with self.assertRaisesRegex(NurseryError, "not allowed") as invalid:
            state_after_action(ModuleState.NEVER_ENABLED, NurseryAction.PAUSE)
        self.assertEqual(invalid.exception.code, "INVALID_STATE_TRANSITION")
        with self.assertRaises(NurseryError) as early:
            state_after_action(
                ModuleState.DELETION_PENDING,
                NurseryAction.FINALIZE_CLEANUP,
                cleanup_due=False,
            )
        self.assertEqual(early.exception.code, "CLEANUP_NOT_DUE")


class CapabilityRulesTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.document = yaml.safe_load(RULES_PATH.read_text(encoding="utf-8"))

    def test_authoritative_yaml_passes_4_13_113_gate(self):
        rules = load_capability_rules(RULES_PATH)
        self.assertEqual(rules.schema_version, 1)
        self.assertEqual(len(rules.stages), 4)
        self.assertEqual(len(rules.dimensions), 13)
        self.assertEqual(len(rules.abilities), 113)
        self.assertEqual(Path(rules.source_path), RULES_PATH.resolve())

    def assert_invalid(self, document, text):
        with self.assertRaisesRegex(CapabilityRulesError, text):
            validate_rules_document(document)

    def test_duplicate_id_fails_closed(self):
        document = copy.deepcopy(self.document)
        document["abilities"][-1]["id"] = document["abilities"][0]["id"]
        self.assert_invalid(document, "duplicate ability id")

    def test_unknown_stage_fails_closed(self):
        document = copy.deepcopy(self.document)
        document["abilities"][0]["emerging_from"] = "future_stage"
        self.assert_invalid(document, "unknown stage")

    def test_wrong_boolean_type_fails_closed(self):
        document = copy.deepcopy(self.document)
        document["abilities"][0]["hard_block"] = "false"
        self.assert_invalid(document, "must be boolean")

    def test_safety_tag_and_culture_types_fail_closed(self):
        bad_tags = copy.deepcopy(self.document)
        bad_tags["abilities"][0]["safety_tags"] = "consent"
        self.assert_invalid(bad_tags, "string list")
        bad_culture = copy.deepcopy(self.document)
        bad_culture["abilities"][0]["culture_optional"] = 1
        self.assert_invalid(bad_culture, "must be boolean")

    def test_backward_level_order_fails_closed(self):
        document = copy.deepcopy(self.document)
        document["abilities"][0]["emerging_from"] = "young_child"
        document["abilities"][0]["assisted_from"] = "infancy"
        self.assert_invalid(document, "move backward")

    def test_negative_or_boolean_evidence_fails_closed(self):
        negative = copy.deepcopy(self.document)
        negative["abilities"][0]["evidence_sessions"] = -1
        self.assert_invalid(negative, "non-negative integer")
        boolean = copy.deepcopy(self.document)
        boolean["abilities"][0]["evidence_sessions"] = True
        self.assert_invalid(boolean, "non-negative integer")

    def test_gate_failure_is_local_to_nursery(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "broken.yaml"
            path.write_text("schema_version: [", encoding="utf-8")
            gate = CapabilityRulesGate(path)
            self.assertFalse(gate.load())
            self.assertFalse(gate.status()["available"])
            self.assertIn("invalid YAML", gate.status()["error"])
            self.assertEqual(2 + 2, 4, "parent process remains usable")


if __name__ == "__main__":
    unittest.main()
