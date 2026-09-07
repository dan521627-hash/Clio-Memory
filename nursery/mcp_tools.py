"""Register the nursery module tools on Anima's existing ``FastMCP`` server.

The nursery is an in-process Anima module: it shares Anima's MCP address and
personal connection, and does not add an endpoint, account, token, or grant.
"""

from __future__ import annotations

import inspect
import json
from typing import Any, Protocol

from .external_tools import EXTERNAL_MCP_PERMISSIONS, ExternalNurseryTools
from .models import ActorContext, AuthSource, CaregiverRole, NurseryError
from .store import NurseryStore


class ExternalMCPActorResolver(Protocol):
    """Resolve Anima's module caregiver without trusting tool arguments."""

    def resolve_external_actor(self) -> ActorContext | Any: ...


class BoundExternalMCPActorResolver:
    """Map Anima's one personal MCP connection to its nursery caregiver.

    This is a semantic shared-draft position inside the same Anima account,
    not a second account or authentication system.
    """

    def __init__(self, store: NurseryStore) -> None:
        self.store = store

    def resolve_external_actor(self) -> ActorContext | None:
        guardian = self.store.single_active_external_guardian()
        if guardian is None:
            return None
        return ActorContext.build(
            account_id=guardian["account_id"],
            caregiver_id=guardian["caregiver_id"],
            role=CaregiverRole.EXTERNAL_AI_GUARDIAN,
            permissions=EXTERNAL_MCP_PERMISSIONS,
            auth_source=AuthSource.MCP_SESSION,
        )


async def _resolve_actor(resolver: ExternalMCPActorResolver) -> ActorContext:
    actor = resolver.resolve_external_actor()
    if inspect.isawaitable(actor):
        actor = await actor
    if not isinstance(actor, ActorContext):
        raise NurseryError(
            "EXTERNAL_GUARDIAN_NOT_BOUND",
            "Anima's nursery caregiver position is not available",
        )
    return actor


def _result(value: dict[str, Any]) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _failure(error: NurseryError) -> str:
    return _result({"ok": False, "error": error.as_dict()})


def register_external_nursery_mcp_tools(
    mcp: Any,
    *,
    tools: ExternalNurseryTools,
    resolver: ExternalMCPActorResolver | None = None,
) -> bool:
    """Register nursery-module tools on Anima's already-running MCP server.

    Calls share Anima's personal MCP trust boundary.  The resolver derives the
    one server-side nursery caregiver; tool arguments cannot choose identity.
    """

    if resolver is None:
        resolver = BoundExternalMCPActorResolver(tools.store)

    @mcp.tool()
    async def nursery_creation_status(child_id: str | None = None) -> str:
        """Read the external guardian's shared Anima nursery creation progress."""
        try:
            actor = await _resolve_actor(resolver)
            return _result({"ok": True, "result": tools.creation_status(actor=actor, child_id=child_id)})
        except NurseryError as error:
            return _failure(error)

    @mcp.tool()
    async def nursery_creation_submit(
        action: str,
        payload: Any = None,
        private_payload: Any = None,
        child_id: str | None = None,
        operation_id: str | None = None,
        idempotency_key: str | None = None,
        expected_state_version: int | None = None,
    ) -> str:
        """Compatibility submit tool.

        Supported creation actions include save_name_proposals,
        review_name_candidates (alias: set_name_preference), select_draft_name
        (aliases: agree_name, agreed_name, finalize_name, set_official_name; each
        still requires payload.candidate_id),
        submit_temperament_questionnaire, submit_initial_style,
        save_initial_space, and confirm_creation. Prefer the dedicated tools below.
        """
        try:
            actor = await _resolve_actor(resolver)
            result = await tools.creation_submit(
                actor=actor,
                child_id=child_id,
                action=action,
                operation_id=operation_id,
                idempotency_key=idempotency_key,
                expected_state_version=expected_state_version,
                payload=payload,
                private_payload=private_payload,
            )
            return _result({"ok": True, "result": result})
        except NurseryError as error:
            return _failure(error)

    @mcp.tool()
    async def nursery_questionnaire_form(kind: str = "all") -> str:
        """Read the exact external-AI questionnaire questions before answering them.

        Call with temperament, initial_style, or all. For every returned id,
        answer with exactly low, middle, or high in the dedicated submit tool.
        """
        try:
            await _resolve_actor(resolver)
            return _result({"ok": True, "result": tools.questionnaire_form(kind=kind)})
        except NurseryError as error:
            return _failure(error)

    @mcp.tool()
    async def nursery_submit_name_opinions(
        opinions: list[dict[str, str]] | dict[str, Any], child_id: str | None = None
    ) -> str:
        """Record your opinion on every current baby-name candidate.

        Each opinion is {"name": "候选名字", "opinion": "like|acceptable|disagree"}.
        The legacy value reject is also accepted. If exactly one candidate is
        accepted by both founding guardians, it becomes the shared draft name
        automatically. If several candidates are jointly accepted, call
        nursery_select_name with the chosen candidate_id.
        Call nursery_creation_status first to see the current candidates.
        """
        try:
            actor = await _resolve_actor(resolver)
            result = await tools.submit_name_opinions(
                actor=actor, child_id=child_id, opinions=opinions
            )
            return _result({"ok": True, "result": result})
        except NurseryError as error:
            return _failure(error)

    @mcp.tool()
    async def nursery_select_name(
        candidate_id: str, child_id: str | None = None
    ) -> str:
        """Choose one candidate when both guardians jointly accept several names.

        Read candidate_id from nursery_creation_status. Either founding guardian
        may perform this action; it is not restricted to the account owner. This
        is the canonical select_draft_name action; both guardians must already
        have accepted the candidate.
        """
        try:
            actor = await _resolve_actor(resolver)
            result = await tools.select_name(
                actor=actor, child_id=child_id, candidate_id=candidate_id
            )
            return _result({"ok": True, "result": result})
        except NurseryError as error:
            return _failure(error)

    @mcp.tool()
    async def nursery_submit_temperament_answers(
        answers: dict[str, str], child_id: str | None = None
    ) -> str:
        """Submit the external AI's six temperament answers privately.

        First call nursery_questionnaire_form with kind=temperament. Provide all
        returned ids with low, middle, or high; no operation id is needed.
        """
        try:
            actor = await _resolve_actor(resolver)
            result = await tools.submit_temperament_answers(
                actor=actor, child_id=child_id, answers=answers
            )
            return _result({"ok": True, "result": result})
        except NurseryError as error:
            return _failure(error)

    @mcp.tool()
    async def nursery_submit_care_style_answers(
        answers: dict[str, str], child_id: str | None = None
    ) -> str:
        """Submit the external AI's six caregiving-style answers privately.

        First call nursery_questionnaire_form with kind=initial_style. Provide
        all returned ids with low, middle, or high; no operation id is needed.
        """
        try:
            actor = await _resolve_actor(resolver)
            result = await tools.submit_care_style_answers(
                actor=actor, child_id=child_id, answers=answers
            )
            return _result({"ok": True, "result": result})
        except NurseryError as error:
            return _failure(error)

    @mcp.tool()
    async def child_status(
        child_id: str | None = None,
        detail: str = "summary",
        since: str | None = None,
        query: str = "",
        cursor: str | None = None,
    ) -> str:
        """Read the external guardian's permitted nursery state.

        detail=conversations reads your own retained child exchange, not the user's private chat.
        detail=care reads shared completed care/corrections and health arrangements.
        detail=family reads shared activities, promises, handoff events and your own relationship.
        Other details: summary, events, world, growth, settings.
        """
        try:
            actor = await _resolve_actor(resolver)
            return _result({"ok": True, "result": tools.child_status(actor=actor, child_id=child_id, detail=detail, since=since, query=query, cursor=cursor)})
        except NurseryError as error:
            return _failure(error)

    @mcp.tool()
    async def child_interact(
        message: str,
        child_id: str | None = None,
        operation_id: str | None = None,
        idempotency_key: str | None = None,
        expected_state_version: int | None = None,
    ) -> str:
        """Send one message to the child through Anima's validated nursery coordinator."""
        try:
            actor = await _resolve_actor(resolver)
            result = await tools.child_interact(
                actor=actor,
                child_id=child_id,
                operation_id=operation_id,
                idempotency_key=idempotency_key,
                expected_state_version=expected_state_version,
                message=message,
            )
            return _result({"ok": True, "result": result})
        except NurseryError as error:
            return _failure(error)

    async def _feature_tool(
        tool_group: str,
        action: str,
        payload: Any = None,
        child_id: str | None = None,
        operation_id: str | None = None,
        idempotency_key: str | None = None,
        expected_state_version: int | None = None,
    ) -> str:
        try:
            actor = await _resolve_actor(resolver)
            result = await tools.feature_update(
                actor=actor,
                tool_group=tool_group,
                action=action,
                payload=payload,
                child_id=child_id,
                operation_id=operation_id,
                idempotency_key=idempotency_key,
                expected_state_version=expected_state_version,
            )
            return _result({"ok": True, "result": result})
        except NurseryError as error:
            return _failure(error)

    @mcp.tool()
    async def child_world_update(
        action: str,
        payload: Any = None,
        child_id: str | None = None,
        operation_id: str | None = None,
        idempotency_key: str | None = None,
        expected_state_version: int | None = None,
    ) -> str:
        """Update room, area, or item facts through the shared coordinator."""
        return await _feature_tool("world", action, payload, child_id, operation_id, idempotency_key, expected_state_version)

    @mcp.tool()
    async def child_profile_update(
        action: str,
        payload: Any = None,
        child_id: str | None = None,
        operation_id: str | None = None,
        idempotency_key: str | None = None,
        expected_state_version: int | None = None,
    ) -> str:
        return await _feature_tool("profile", action, payload, child_id, operation_id, idempotency_key, expected_state_version)

    @mcp.tool()
    async def child_growth(
        action: str,
        payload: Any = None,
        child_id: str | None = None,
        operation_id: str | None = None,
        idempotency_key: str | None = None,
        expected_state_version: int | None = None,
    ) -> str:
        """Read child_status(detail='growth') before recording or confirming growth.

        record_growth_observation: payload={observation:{session_id,ability_ids,
        observed_behavior,assistance}}. Choose only IDs with can_record=true in
        the returned catalog; observations never imply mastery or stage change.
        propose_stage: {target_stage}. confirm_stage: {proposal_id,proposal_version}
        from an open proposal; the user and this authenticated AI confirm separately.
        withdraw_stage_proposal: {proposal_id}, only by the proposal's author.
        Keep operation_id/idempotency_key unchanged when retrying the same action.
        """
        return await _feature_tool("growth", action, payload, child_id, operation_id, idempotency_key, expected_state_version)

    @mcp.tool()
    async def child_care(
        action: str,
        payload: Any = None,
        child_id: str | None = None,
        operation_id: str | None = None,
        idempotency_key: str | None = None,
        expected_state_version: int | None = None,
    ) -> str:
        """Care and shared family updates. Read child_status(detail='family' or 'care') first.

        update_family_thread: payload {kind: activity|promise|handoff, title, summary,
        status: active|paused|discuss|completed|cancelled}; existing threads also require
        thread_id and expected_version from family view. Only completed activities may
        include artifact_name. Do not overwrite a stale version or complete another's promise.
        record_relationship_event: {kind: companionship|reassurance|misunderstanding|repair,
        summary: observed interaction, session_id: stable evidence session}. Never rank caregivers.
        set_rest_state: {sleeping: boolean, summary: observed rest or wake event}.
        correct_care_event: {care_event_id, reason}; correct your own mistaken record,
        never erase later care or use this to reverse medical care.
        Existing record_care_observation, comfort_care and execute_confirmed_care remain available.
        """
        return await _feature_tool("care", action, payload, child_id, operation_id, idempotency_key, expected_state_version)

    @mcp.tool()
    async def child_control(
        action: str,
        payload: Any = None,
        child_id: str | None = None,
        operation_id: str | None = None,
        idempotency_key: str | None = None,
        expected_state_version: int | None = None,
    ) -> str:
        return await _feature_tool("control", action, payload, child_id, operation_id, idempotency_key, expected_state_version)

    return True
