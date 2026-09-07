from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from pathlib import Path

from nursery.external_tools import EXTERNAL_TOOL_PERMISSION, ExternalNurseryTools
from nursery.mcp_tools import register_external_nursery_mcp_tools
from nursery.models import (
    ActorContext,
    AuthSource,
    CaregiverRole,
    ModuleState,
    NurseryAction,
    NurseryError,
    OperationResult,
    OperationStatus,
)
from nursery.store import NurseryStore


class ExternalToolBoundaryTests(unittest.TestCase):
    def test_name_selection_compatibility_aliases_keep_the_consensus_action(self):
        for action in ("agree_name", "agreed_name", "finalize_name", "set_official_name"):
            with self.subTest(action=action):
                self.assertEqual(
                    ExternalNurseryTools._action(action, {}),
                    NurseryAction.SELECT_DRAFT_NAME,
                )

    def test_each_mcp_feature_tool_has_its_own_action_boundary(self):
        class FakeMCP:
            def __init__(self):
                self.functions = {}

            def tool(self):
                def register(function):
                    self.functions[function.__name__] = function
                    return function
                return register

        class CaptureCreation:
            def __init__(self):
                self.commands = []

            async def submit(self, command):
                self.commands.append(command)
                return OperationResult(
                    operation_id=command.operation_id,
                    status=OperationStatus.COMPLETED,
                )

        with tempfile.TemporaryDirectory() as root:
            store = NurseryStore(Path(root) / "nursery.sqlite3")
            store.create_fixture(account_id="account", child_id="child", state=ModuleState.ACTIVE)
            actor = ActorContext.build(
                account_id="account",
                caregiver_id="external-guardian",
                role=CaregiverRole.EXTERNAL_AI_GUARDIAN,
                permissions=(EXTERNAL_TOOL_PERMISSION,),
                auth_source=AuthSource.MCP_SESSION,
            )
            store.register_caregiver(actor)
            creation = CaptureCreation()

            class Resolver:
                def resolve_external_actor(self):
                    return actor

            mcp = FakeMCP()
            register_external_nursery_mcp_tools(
                mcp,
                tools=ExternalNurseryTools(store=store, creation=creation),
                resolver=Resolver(),
            )
            allowed = {
                "child_world_update": ("add_area", NurseryAction.ADD_AREA),
                "child_profile_update": ("propose_name_change", NurseryAction.PROPOSE_NAME_CHANGE),
                "child_growth": ("record_observation", NurseryAction.RECORD_GROWTH_OBSERVATION),
                "child_care": ("record_observation", NurseryAction.RECORD_CARE_OBSERVATION),
                "child_control": ("pause", NurseryAction.PAUSE),
            }
            blocked = {
                "child_world_update": "pause",
                "child_profile_update": "add_item",
                "child_growth": "comfort",
                "child_care": "propose_stage",
                "child_control": "update_own_calling_preference",
            }
            for tool_name, (action, expected) in allowed.items():
                with self.subTest(tool=tool_name, action=action):
                    response = json.loads(asyncio.run(mcp.functions[tool_name](action=action)))
                    self.assertTrue(response["ok"], response)
                    self.assertEqual(creation.commands[-1].action, expected)
            for tool_name, action in blocked.items():
                with self.subTest(tool=tool_name, action=action):
                    response = json.loads(asyncio.run(mcp.functions[tool_name](action=action)))
                    self.assertFalse(response["ok"])
                    self.assertEqual(
                        response["error"]["code"], "EXTERNAL_TOOL_ACTION_NOT_AVAILABLE"
                    )

    def test_external_tool_requires_trusted_external_guardian(self):
        with tempfile.TemporaryDirectory() as root:
            store = NurseryStore(Path(root) / "nursery.sqlite3")
            store.create_fixture(account_id="account", child_id="child", state=ModuleState.ACTIVE)
            actor = ActorContext.build(
                account_id="account",
                caregiver_id="user-guardian",
                role=CaregiverRole.USER_GUARDIAN,
                permissions=("nursery.*",),
                auth_source=AuthSource.MANAGER_SESSION,
            )
            tools = ExternalNurseryTools(store=store, creation=None)  # type: ignore[arg-type]
            with self.assertRaises(NurseryError) as rejected:
                tools.child_status(actor=actor, child_id="child")
            self.assertEqual(rejected.exception.code, "EXTERNAL_GUARDIAN_REQUIRED")

    def test_external_tool_requires_authenticated_mcp_context_and_scope(self):
        with tempfile.TemporaryDirectory() as root:
            store = NurseryStore(Path(root) / "nursery.sqlite3")
            store.create_fixture(account_id="account", child_id="child", state=ModuleState.ACTIVE)
            actor = ActorContext.build(
                account_id="account",
                caregiver_id="external-guardian",
                role=CaregiverRole.EXTERNAL_AI_GUARDIAN,
                permissions=(EXTERNAL_TOOL_PERMISSION,),
                auth_source=AuthSource.TEST_FIXTURE,
            )
            tools = ExternalNurseryTools(store=store, creation=None)  # type: ignore[arg-type]
            with self.assertRaises(NurseryError) as rejected:
                tools.child_status(actor=actor, child_id="child")
            self.assertEqual(rejected.exception.code, "EXTERNAL_TOOL_AUTH_REQUIRED")

    def test_mcp_registration_takes_identity_from_resolver_not_tool_arguments(self):
        class FakeMCP:
            def __init__(self):
                self.tools = []

            def tool(self):
                def register(function):
                    self.tools.append(function.__name__)
                    return function
                return register

        class Resolver:
            def resolve_external_actor(self):
                return None

        with tempfile.TemporaryDirectory() as root:
            store = NurseryStore(Path(root) / "nursery.sqlite3")
            runtime_tools = ExternalNurseryTools(store=store, creation=None)  # type: ignore[arg-type]
            mcp = FakeMCP()
            register_external_nursery_mcp_tools(mcp, tools=runtime_tools, resolver=Resolver())
            self.assertEqual(
                mcp.tools,
                [
                    "nursery_creation_status",
                    "nursery_creation_submit",
                    "nursery_questionnaire_form",
                    "nursery_submit_name_opinions",
                    "nursery_select_name",
                    "nursery_submit_temperament_answers",
                    "nursery_submit_care_style_answers",
                    "child_status",
                    "child_interact",
                    "child_world_update",
                    "child_profile_update",
                    "child_growth",
                    "child_care",
                    "child_control",
                ],
            )


if __name__ == "__main__":
    unittest.main()
