"""Trusted-adapter surface for Anima's personal external-AI nursery tools.

The transport derives the external guardian from Anima's existing MCP entry.
Tool input never supplies a caregiver id, account id, model key, or database
path.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from typing import Any

from .creation_queries import NurseryCreationQueryService
from .creation_service import NurseryCreationService
from .body_state import BodyTimeRule
from .interaction_service import NurseryInteractionService
from .models import (
    ANIMA_MCP_ALLOWED_ACTIONS,
    ANIMA_MCP_TOOL_PERMISSION,
    ActorContext,
    CaregiverRole,
    NurseryAction,
    NurseryCommand,
    NurseryError,
    SourceReference,
    SourceType,
)
from .queries import NurseryQueryService
from .store import NurseryStore


EXTERNAL_TOOL_PERMISSION = ANIMA_MCP_TOOL_PERMISSION

EXTERNAL_CREATION_ACTIONS = frozenset(
    action
    for action in ANIMA_MCP_ALLOWED_ACTIONS
    if action != NurseryAction.CHILD_INTERACT
)

EXTERNAL_MCP_PERMISSIONS = frozenset({EXTERNAL_TOOL_PERMISSION})

TOOL_CATALOG = (
    {"name": "nursery_creation_status", "mode": "read", "available": True},
    {"name": "nursery_creation_submit", "mode": "write", "available": True},
    {"name": "nursery_select_name", "mode": "write", "available": True},
    {"name": "nursery_questionnaire_form", "mode": "read", "available": True},
    {"name": "nursery_submit_name_opinions", "mode": "write", "available": True},
    {"name": "nursery_submit_temperament_answers", "mode": "write", "available": True},
    {"name": "nursery_submit_care_style_answers", "mode": "write", "available": True},
    {"name": "child_status", "mode": "read", "available": True},
    {"name": "child_interact", "mode": "write", "available": True, "requires": "ready_child_model"},
    {"name": "child_world_update", "mode": "write", "available": True},
    {"name": "child_profile_update", "mode": "write", "available": True},
    {"name": "child_growth", "mode": "write", "available": True},
    {"name": "child_care", "mode": "write", "available": True},
    {"name": "child_control", "mode": "write", "available": True},
)

_ACTION_ALIASES = {
    # Creation clients used these natural-language names before the dedicated
    # nursery_select_name tool was available.  They still require candidate_id
    # and the same two-guardian consensus as select_draft_name.
    "agree_name": NurseryAction.SELECT_DRAFT_NAME,
    "agreed_name": NurseryAction.SELECT_DRAFT_NAME,
    "finalize_name": NurseryAction.SELECT_DRAFT_NAME,
    "set_official_name": NurseryAction.SELECT_DRAFT_NAME,
    "set_name_preference": NurseryAction.REVIEW_NAME_CANDIDATES,
    "vote_name": NurseryAction.REVIEW_NAME_CANDIDATES,
    "name_vote": NurseryAction.REVIEW_NAME_CANDIDATES,
    "review_name": NurseryAction.REVIEW_NAME_CANDIDATES,
    "review_names": NurseryAction.REVIEW_NAME_CANDIDATES,
    "submit_temperament": NurseryAction.SUBMIT_TEMPERAMENT_QUESTIONNAIRE,
    "temperament_questionnaire": NurseryAction.SUBMIT_TEMPERAMENT_QUESTIONNAIRE,
    "submit_care_style": NurseryAction.SUBMIT_INITIAL_STYLE,
    "care_style_questionnaire": NurseryAction.SUBMIT_INITIAL_STYLE,
}

FEATURE_ACTIONS = {
    "world": frozenset(
        {
            NurseryAction.ADD_AREA,
            NurseryAction.UPDATE_AREA,
            NurseryAction.ADD_ITEM,
            NurseryAction.MOVE_ITEM,
            NurseryAction.UPDATE_ITEM,
            NurseryAction.STORE_ITEM,
            NurseryAction.REMOVE_ITEM,
        }
    ),
    "profile": frozenset(
        {
            NurseryAction.PROPOSE_NAME_CHANGE,
            NurseryAction.CONFIRM_NAME_CHANGE,
            NurseryAction.WITHDRAW_NAME_CHANGE,
            NurseryAction.UPDATE_OWN_CALLING_PREFERENCE,
        }
    ),
    "growth": frozenset(
        {
            NurseryAction.RECORD_GROWTH_OBSERVATION,
            NurseryAction.PROPOSE_STAGE,
            NurseryAction.CONFIRM_STAGE,
            NurseryAction.WITHDRAW_STAGE_PROPOSAL,
        }
    ),
    "care": frozenset(
        {
            NurseryAction.RECORD_CARE_OBSERVATION,
            NurseryAction.COMFORT_CARE,
            NurseryAction.SET_REST_STATE,
            NurseryAction.CORRECT_CARE_EVENT,
            NurseryAction.UPDATE_FAMILY_THREAD,
            NurseryAction.RECORD_RELATIONSHIP_EVENT,
            NurseryAction.EXECUTE_CONFIRMED_CARE,
        }
    ),
    "control": frozenset(
        {
            NurseryAction.PAUSE,
            NurseryAction.RESUME,
            NurseryAction.REQUEST_DELETION,
            NurseryAction.CONFIRM_DELETION,
            NurseryAction.CANCEL_DELETION,
        }
    ),
}

FEATURE_ACTION_ALIASES = {
    "growth": {
        "record_observation": NurseryAction.RECORD_GROWTH_OBSERVATION,
        "withdraw_proposal": NurseryAction.WITHDRAW_STAGE_PROPOSAL,
    },
    "care": {
        "record_observation": NurseryAction.RECORD_CARE_OBSERVATION,
        "comfort": NurseryAction.COMFORT_CARE,
        "execute_confirmed_plan": NurseryAction.EXECUTE_CONFIRMED_CARE,
    },
    "control": {
        "request_delete": NurseryAction.REQUEST_DELETION,
        "confirm_delete": NurseryAction.CONFIRM_DELETION,
        "cancel_delete": NurseryAction.CANCEL_DELETION,
    },
}

_QUESTIONNAIRE_FORMS = {
    "temperament": (
        ("notice_and_strangers", "孩子面对陌生变化和周围气氛时，通常会怎样？"),
        ("daily_activity", "孩子在日常里更像哪一种？"),
        ("closeness_preference", "孩子需要熟悉陪伴时，更接近哪一种？"),
        ("emotion_and_soothing", "孩子有情绪时，更接近哪一种？"),
        ("adapt_to_change", "遇到新的人、地方或安排时，孩子会怎样？"),
        ("stay_with_failure", "第一次没做好时，孩子更可能怎样？"),
    ),
    "initial_style": (
        ("respond_to_crying", "孩子哭的时候，你通常会怎么做？"),
        ("respect_refusal", "孩子说不想或拒绝时，你更接近哪一种回应？"),
        ("handle_mistakes", "孩子做错或受挫时，你会怎样陪他？"),
        ("encouragement", "你通常怎样鼓励孩子？"),
        ("conflict_repair", "发生小冲突后，你更愿意怎样修复？"),
        ("create_safety", "你会怎样让孩子感到安全？"),
    ),
}


@dataclass(frozen=True)
class ExternalNurseryTools:
    """The callable core behind a future authenticated MCP adapter."""

    store: NurseryStore
    creation: NurseryCreationService
    interaction: NurseryInteractionService | None = None
    body_time_rule: BodyTimeRule | None = None

    def _require_guardian(self, actor: ActorContext) -> None:
        if actor.role != CaregiverRole.EXTERNAL_AI_GUARDIAN:
            raise NurseryError(
                "EXTERNAL_GUARDIAN_REQUIRED",
                "this tool requires the authenticated external AI guardian",
            )
        if actor.auth_source.value != "mcp_session":
            raise NurseryError(
                "EXTERNAL_TOOL_AUTH_REQUIRED",
                "external nursery tools require an authenticated MCP actor context",
            )
        if not actor.has_permission(EXTERNAL_TOOL_PERMISSION):
            raise NurseryError(
                "PERMISSION_DENIED",
                "external nursery tool access is not authorized for this actor",
            )
        self.store.assert_actor_registered(actor)

    def _child_id(self, actor: ActorContext, proposed_child_id: str | None) -> str:
        requested = str(proposed_child_id or "").strip()
        if requested:
            return requested
        child_id = self.store.child_id_for_account(actor.account_id)
        if not child_id:
            raise NurseryError("NURSERY_NOT_FOUND", "no nursery child is available")
        return child_id

    @staticmethod
    def _object(value: Any, *, field: str) -> dict[str, Any]:
        if value is None:
            return {}
        if isinstance(value, dict):
            return dict(value)
        if isinstance(value, str):
            try:
                decoded = json.loads(value)
            except json.JSONDecodeError as error:
                raise NurseryError("INVALID_TOOL_INPUT", f"{field} must be an object") from error
            if isinstance(decoded, dict):
                return dict(decoded)
        raise NurseryError("INVALID_TOOL_INPUT", f"{field} must be an object")

    @staticmethod
    def _action(value: Any, payload: dict[str, Any]) -> NurseryAction:
        raw = str(value or "").strip().lower().replace("-", "_").replace(" ", "_")
        if raw == "submit_questionnaire":
            kind = str(payload.get("questionnaire_kind") or "").strip().lower()
            if kind in {"temperament", "child_temperament"}:
                return NurseryAction.SUBMIT_TEMPERAMENT_QUESTIONNAIRE
            if kind in {"initial_style", "care_style", "parenting_style"}:
                return NurseryAction.SUBMIT_INITIAL_STYLE
            raise NurseryError(
                "QUESTIONNAIRE_KIND_REQUIRED",
                "submit_questionnaire needs questionnaire_kind: temperament or initial_style",
            )
        if raw in _ACTION_ALIASES:
            return _ACTION_ALIASES[raw]
        try:
            return NurseryAction(raw)
        except ValueError as error:
            raise NurseryError(
                "INVALID_NURSERY_ACTION",
                "use a named nursery action or one of the dedicated nursery tools",
            ) from error

    @staticmethod
    def _feature_action(value: Any, *, tool_group: str) -> NurseryAction:
        allowed = FEATURE_ACTIONS.get(tool_group)
        if allowed is None:
            raise NurseryError("INVALID_FEATURE_TOOL", "nursery feature tool is invalid")
        raw = str(value or "").strip().lower().replace("-", "_").replace(" ", "_")
        requested = FEATURE_ACTION_ALIASES.get(tool_group, {}).get(raw)
        if requested is None:
            requested = next(
                (aliases[raw] for aliases in FEATURE_ACTION_ALIASES.values() if raw in aliases),
                None,
            )
        if requested is None:
            try:
                requested = NurseryAction(raw)
            except ValueError as error:
                raise NurseryError(
                    "INVALID_NURSERY_ACTION",
                    "use an action supported by this nursery feature tool",
                ) from error
        if requested not in allowed:
            raise NurseryError(
                "EXTERNAL_TOOL_ACTION_NOT_AVAILABLE",
                "this action is not available through this feature tool",
            )
        return requested

    async def _submit(
        self,
        *,
        actor: ActorContext,
        child_id: str | None,
        action: NurseryAction,
        payload: dict[str, Any],
        private_payload: dict[str, Any] | None,
        operation_id: str | None = None,
        idempotency_key: str | None = None,
        expected_state_version: int | None = None,
    ) -> dict[str, Any]:
        target_child_id = self._child_id(actor, child_id)
        operation = str(operation_id or "").strip() or str(uuid.uuid4())
        idempotency = str(idempotency_key or "").strip() or operation
        command = NurseryCommand(
            actor=actor,
            child_id=target_child_id,
            action=action,
            operation_id=operation,
            idempotency_key=idempotency,
            expected_state_version=expected_state_version,
            payload=payload,
            private_payload=private_payload,
            source=SourceReference.build(
                source_type=SourceType.EXTERNAL_AI_INTERACTION,
                source_id=f"external-ai:nursery:{operation}",
                source_version="v1",
                specification_ref="nursery-external-tools-v1",
            ),
        )
        return (await self.creation.submit(command)).as_dict()

    def creation_status(self, *, actor: ActorContext, child_id: str | None = None) -> dict[str, Any]:
        self._require_guardian(actor)
        return NurseryCreationQueryService(self.store).get_draft(
            actor=actor, child_id=self._child_id(actor, child_id)
        )

    def child_status(
        self,
        *,
        actor: ActorContext,
        child_id: str | None = None,
        detail: str = "summary",
        since: str | None = None,
        query: str = "",
        cursor: str | None = None,
    ) -> dict[str, Any]:
        self._require_guardian(actor)
        return NurseryQueryService(
            self.store, body_time_rule=self.body_time_rule,
            capability_rules=self.creation.capability_rules if self.creation is not None else None,
        ).get_child_status(
            actor=actor,
            child_id=self._child_id(actor, child_id),
            detail=detail,
            since=since,
            query=query,
            cursor=cursor,
        )

    async def feature_update(
        self,
        *,
        actor: ActorContext,
        tool_group: str,
        action: NurseryAction | str,
        payload: Any = None,
        child_id: str | None = None,
        operation_id: str | None = None,
        idempotency_key: str | None = None,
        expected_state_version: int | None = None,
    ) -> dict[str, Any]:
        """Submit one post-creation feature action through the shared coordinator."""

        self._require_guardian(actor)
        requested = self._feature_action(action, tool_group=tool_group)
        return await self._submit(
            actor=actor,
            child_id=child_id,
            action=requested,
            payload=self._object(payload, field="payload"),
            private_payload=None,
            operation_id=operation_id,
            idempotency_key=idempotency_key,
            expected_state_version=expected_state_version,
        )

    async def creation_submit(
        self,
        *,
        actor: ActorContext,
        child_id: str | None,
        action: NurseryAction | str,
        operation_id: str | None = None,
        idempotency_key: str | None = None,
        expected_state_version: int | None = None,
        payload: Any = None,
        private_payload: Any = None,
    ) -> dict[str, Any]:
        self._require_guardian(actor)
        public = self._object(payload, field="payload")
        private = self._object(private_payload, field="private_payload") if private_payload is not None else None
        requested = self._action(action, public)
        if requested not in EXTERNAL_CREATION_ACTIONS:
            raise NurseryError(
                "EXTERNAL_TOOL_ACTION_NOT_AVAILABLE",
                "this action is not available to the external AI tool adapter",
            )
        if requested in {
            NurseryAction.SUBMIT_TEMPERAMENT_QUESTIONNAIRE,
            NurseryAction.SUBMIT_INITIAL_STYLE,
        } and private is None and "answers" in public:
            private = {"answers": public.pop("answers")}
        return await self._submit(
            actor=actor,
            child_id=child_id,
            action=requested,
            payload=public,
            private_payload=private,
            operation_id=operation_id,
            idempotency_key=idempotency_key,
            expected_state_version=expected_state_version,
        )

    def questionnaire_form(self, *, kind: str = "all") -> dict[str, Any]:
        selected = str(kind or "all").strip().lower()
        if selected not in {"all", "temperament", "initial_style"}:
            raise NurseryError("INVALID_QUESTIONNAIRE_KIND", "kind must be temperament, initial_style, or all")
        kinds = _QUESTIONNAIRE_FORMS if selected == "all" else {selected: _QUESTIONNAIRE_FORMS[selected]}
        return {
            "answer_values": ["low", "middle", "high"],
            "forms": {
                form_kind: [
                    {"id": question_id, "prompt": prompt}
                    for question_id, prompt in questions
                ]
                for form_kind, questions in kinds.items()
            },
        }

    async def submit_name_opinions(
        self,
        *,
        actor: ActorContext,
        opinions: Any,
        child_id: str | None = None,
    ) -> dict[str, Any]:
        self._require_guardian(actor)
        target_child_id = self._child_id(actor, child_id)
        items = list(opinions.values()) if isinstance(opinions, dict) else opinions
        if not isinstance(items, list) or not items:
            raise NurseryError("INVALID_NAME_PREFERENCES", "opinions must be a non-empty list")
        draft = NurseryCreationQueryService(self.store).get_draft(actor=actor, child_id=target_child_id)
        candidates = draft.get("name_candidates") or []
        by_name = {str(item.get("proposed_name") or ""): str(item.get("candidate_id") or "") for item in candidates}
        preferences: dict[str, str] = {}
        for item in items:
            if not isinstance(item, dict):
                raise NurseryError("INVALID_NAME_PREFERENCES", "each opinion must be an object")
            candidate_id = str(item.get("candidate_id") or "").strip()
            if not candidate_id:
                candidate_id = by_name.get(str(item.get("name") or "").strip(), "")
            if not candidate_id:
                raise NurseryError("NAME_CANDIDATE_NOT_FOUND", "each opinion needs a current name or candidate_id")
            preferences[candidate_id] = str(item.get("opinion") or item.get("preference") or item.get("vote") or "").strip()
        return await self._submit(
            actor=actor,
            child_id=target_child_id,
            action=NurseryAction.REVIEW_NAME_CANDIDATES,
            payload={"preferences": preferences},
            private_payload=None,
        )

    async def select_name(
        self,
        *,
        actor: ActorContext,
        candidate_id: str,
        child_id: str | None = None,
    ) -> dict[str, Any]:
        """Resolve multiple jointly accepted candidates without an owner-only step."""

        self._require_guardian(actor)
        selected = str(candidate_id or "").strip()
        if not selected:
            raise NurseryError("NAME_CANDIDATE_NOT_FOUND", "candidate_id is required")
        return await self._submit(
            actor=actor,
            child_id=child_id,
            action=NurseryAction.SELECT_DRAFT_NAME,
            payload={"candidate_id": selected},
            private_payload=None,
        )

    async def submit_temperament_answers(
        self, *, actor: ActorContext, answers: Any, child_id: str | None = None
    ) -> dict[str, Any]:
        self._require_guardian(actor)
        return await self._submit(
            actor=actor,
            child_id=child_id,
            action=NurseryAction.SUBMIT_TEMPERAMENT_QUESTIONNAIRE,
            payload={},
            private_payload={"answers": self._object(answers, field="answers")},
        )

    async def submit_care_style_answers(
        self, *, actor: ActorContext, answers: Any, child_id: str | None = None
    ) -> dict[str, Any]:
        self._require_guardian(actor)
        return await self._submit(
            actor=actor,
            child_id=child_id,
            action=NurseryAction.SUBMIT_INITIAL_STYLE,
            payload={},
            private_payload={"answers": self._object(answers, field="answers")},
        )

    async def child_interact(
        self,
        *,
        actor: ActorContext,
        message: str,
        child_id: str | None = None,
        operation_id: str | None = None,
        idempotency_key: str | None = None,
        expected_state_version: int | None = None,
    ) -> dict[str, Any]:
        self._require_guardian(actor)
        if self.interaction is None:
            raise NurseryError(
                "CHILD_INTERACTION_NOT_CONFIGURED",
                "child interaction service is not configured",
            )
        operation = str(operation_id or "").strip() or str(uuid.uuid4())
        idempotency = str(idempotency_key or "").strip() or operation
        return await self.interaction.interact(
            actor=actor,
            child_id=self._child_id(actor, child_id),
            operation_id=operation,
            idempotency_key=idempotency,
            expected_state_version=expected_state_version,
            message=message,
            source=SourceReference.build(
                source_type=SourceType.EXTERNAL_AI_INTERACTION,
                source_id=f"external-ai:nursery:{operation}",
                source_version="v1",
                specification_ref="nursery-external-tools-v1",
            ),
        )
