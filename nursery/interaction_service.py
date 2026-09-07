"""Stage-3 child dialogue through the existing nursery coordinator."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import timedelta
from typing import Any

from .coordinator import NurseryCoordinator, PreparedMutation
from .body_state import BodyTimeRule
from .care_rules import normalize_care_action
from .health_runtime import NurseryHealthRuntime
from .model_connection import (
    AdapterRegistry,
    ChildConversationProposal,
    ChildConversationRequest,
    ModelConnectionRequest,
    default_adapter_registry,
    validate_connection_request,
)
from .models import (
    ActorContext,
    CaregiverRole,
    NurseryAction,
    NurseryCommand,
    NurseryError,
    SourceReference,
)
from .secret_vault import SecretVault
from .store import NurseryStore, iso_time


# A model may describe the immediate emotional tone, never bodily needs or a
# health fact.  Those values are respectively time-settled and source-backed.
MODEL_RUNTIME_FIELDS = frozenset({"comfort", "connection", "play_drive"})
RUNTIME_NUMBER_FIELDS = frozenset(
    {"thirst", "hunger", "fatigue", *MODEL_RUNTIME_FIELDS, "unwell"}
)
ALLOWED_INTENTS = frozenset(
    {"talk", "drink", "food", "rest", "play", "comfort", "quiet", "none"}
)
MAX_RUNTIME_DELTA = 0.35
SHORT_EVENT_RETENTION_DAYS = 30


class NurseryInteractionService:
    """One turn in, one validated child reply and state update out."""

    def __init__(
        self,
        store: NurseryStore,
        *,
        vault: SecretVault,
        adapter_registry: AdapterRegistry | None = None,
        body_time_rule: BodyTimeRule | None = None,
        health_runtime: NurseryHealthRuntime | None = None,
    ) -> None:
        self.store = store
        self.vault = vault
        self.adapter_registry = adapter_registry or default_adapter_registry()
        self.body_time_rule = body_time_rule or BodyTimeRule()
        self.health_runtime = health_runtime or NurseryHealthRuntime(store)
        try:
            self.body_time_rule.validate()
        except ValueError as exc:
            raise NurseryError("INVALID_BODY_RULE", str(exc)) from exc
        self.coordinator = NurseryCoordinator(
            store, prepare_hook=self._prepare, body_time_rule=self.body_time_rule
        )

    @staticmethod
    def _message(value: Any) -> str:
        message = " ".join(str(value or "").split())
        if not message or len(message) > 1200:
            raise NurseryError(
                "INVALID_CHILD_MESSAGE", "child interaction message must contain 1 to 1200 characters"
            )
        return message

    @staticmethod
    def _conversation_channel(actor: ActorContext) -> str:
        if actor.role == CaregiverRole.EXTERNAL_AI_GUARDIAN:
            return "external_ai_child"
        if actor.role == CaregiverRole.USER_GUARDIAN:
            return "user_child"
        return "shared_family"

    @staticmethod
    def _emotional_state(previous: dict[str, Any], proposed: Any) -> dict[str, Any]:
        """Keep a compact, bounded feeling state without inventing body facts."""

        fallback = previous.get("emotion")
        current = dict(fallback) if isinstance(fallback, dict) else {}
        current_secondary = current.get("secondary", [])
        if not isinstance(current_secondary, list):
            current_secondary = []
        current = {
            "primary": " ".join(str(current.get("primary") or "安稳").split())[:24] or "安稳",
            "secondary": [
                " ".join(str(item).split())[:24]
                for item in current_secondary
                if " ".join(str(item).split())[:24]
            ][:3],
            "valence": float(current.get("valence", 0.35)),
            "arousal": float(current.get("arousal", 0.3)),
            "safety": float(current.get("safety", 0.8)),
            "cause": " ".join(str(current.get("cause") or "").split())[:160],
        }
        if proposed is None or proposed == {}:
            return current
        if not isinstance(proposed, dict) or set(proposed) - set(current):
            raise NurseryError("INVALID_CHILD_MODEL_OUTPUT", "child emotion proposal is invalid")
        def chinese_label(value: Any) -> str:
            label = " ".join(str(value or "").split())
            translated = {
                "calm": "平静", "content": "满足", "happy": "开心",
                "joyful": "快乐", "excited": "兴奋", "curious": "好奇",
                "playful": "想玩", "relaxed": "放松", "safe": "安心",
                "secure": "安心", "sleepy": "困倦", "tired": "疲倦",
                "hungry": "饿了", "thirsty": "渴了", "sad": "难过",
                "upset": "不开心", "anxious": "不安", "worried": "担心",
                "lonely": "孤单", "uncomfortable": "不舒服",
            }.get(label.lower())
            return translated or label

        primary = chinese_label(proposed.get("primary") or current["primary"])
        secondary_raw = proposed.get("secondary", current["secondary"])
        cause = " ".join(str(proposed.get("cause") or current["cause"]).split())
        if not primary or len(primary) > 24 or not isinstance(secondary_raw, list) or len(secondary_raw) > 3 or len(cause) > 160:
            raise NurseryError("INVALID_CHILD_MODEL_OUTPUT", "child emotion proposal is invalid")
        secondary = [chinese_label(item) for item in secondary_raw]
        if any(not item or len(item) > 24 for item in secondary):
            raise NurseryError("INVALID_CHILD_MODEL_OUTPUT", "child emotion proposal is invalid")
        values = {
            "valence": proposed.get("valence", current["valence"]),
            "arousal": proposed.get("arousal", current["arousal"]),
            "safety": proposed.get("safety", current["safety"]),
        }
        if (
            any(type(value) not in {int, float} for value in values.values())
            or not -1 <= float(values["valence"]) <= 1
            or not 0 <= float(values["arousal"]) <= 1
            or not 0 <= float(values["safety"]) <= 1
        ):
            raise NurseryError("INVALID_CHILD_MODEL_OUTPUT", "child emotion proposal is invalid")
        return {
            "primary": primary,
            "secondary": secondary,
            "valence": round(float(values["valence"]), 3),
            "arousal": round(float(values["arousal"]), 3),
            "safety": round(float(values["safety"]), 3),
            "cause": cause,
        }

    @staticmethod
    def _validate_proposal(
        previous: dict[str, Any], proposal: ChildConversationProposal,
        *, tolerate_invalid_care: bool = True,
    ) -> dict[str, Any]:
        reply = " ".join(str(proposal.reply or "").split())
        if not reply or len(reply) > 500:
            raise NurseryError("INVALID_CHILD_MODEL_OUTPUT", "child reply is invalid")
        intent = str(proposal.intent or "").strip().lower()
        if intent not in ALLOWED_INTENTS:
            raise NurseryError("INVALID_CHILD_MODEL_OUTPUT", "child intent is invalid")
        proposed = proposal.runtime_state
        if not isinstance(proposed, dict) or set(proposed) - RUNTIME_NUMBER_FIELDS:
            raise NurseryError("INVALID_CHILD_MODEL_OUTPUT", "child state proposal is invalid")
        state = dict(previous)
        for field in MODEL_RUNTIME_FIELDS:
            before = float(previous.get(field, 0.0))
            value = proposed.get(field, before)
            if type(value) not in {int, float} or not 0 <= float(value) <= 1:
                raise NurseryError("INVALID_CHILD_MODEL_OUTPUT", "child state value is invalid")
            if abs(float(value) - before) > MAX_RUNTIME_DELTA:
                raise NurseryError("INVALID_CHILD_MODEL_OUTPUT", "child state change is too large")
            state[field] = round(float(value), 3)
        # The response model cannot manufacture, worsen, or clear bodily or
        # health facts.  Body time and health/care services own these values.
        for field in ("thirst", "hunger", "fatigue"):
            state[field] = round(float(previous.get(field, 0.0)), 3)
        state["unwell"] = round(float(previous.get("unwell", 0.0)), 3)
        state["last_intent"] = intent
        try:
            state["emotion"] = NurseryInteractionService._emotional_state(
                previous, proposal.emotional_state
            )
        except NurseryError as error:
            if error.code != "INVALID_CHILD_MODEL_OUTPUT":
                raise
            state["emotion"] = dict(previous.get("emotion") or {})
        try:
            care_action = normalize_care_action(proposal.care_action)
        except NurseryError as error:
            if not tolerate_invalid_care or error.code != "INVALID_CHILD_MODEL_OUTPUT":
                raise
            # A malformed optional care record must never swallow an otherwise
            # safe child reply. No care/body mutation is written in this case.
            care_action = {"kind": "none", "status": "none", "object_name": "", "summary": ""}
        return {
            "reply": reply,
            "intent": intent,
            "runtime_state": state,
            "care_action": care_action,
        }

    @staticmethod
    def _clear_completed_care_from_message(message: str, caregiver_name: str) -> dict[str, str] | None:
        """Recover only explicit completed everyday care missed by the model.

        Questions, offers and future plans deliberately stay out. This is the
        same narrow fallback idea used by automatic Anima write extraction.
        """
        text = " ".join(str(message or "").split())
        if not text or any(marker in text for marker in ("要不要", "想不想", "可以吗", "好吗", "？", "?", "如果", "准备", "等会", "一会")):
            return None
        completed = any(marker in text for marker in ("已经", "刚刚", "刚才", "喝下", "喝完", "吃完", "吃下", "抱了抱", "拍了拍"))
        if not completed:
            return None
        name = caregiver_name or "养育者"
        if any(marker in text for marker in ("喝下", "喝完", "喝了")):
            return {"kind": "drink", "status": "completed", "object_name": "水", "summary": f"{name}已经照顾孩子喝了水。"}
        if any(marker in text for marker in ("吃完", "吃下", "吃了")):
            return {"kind": "food", "status": "completed", "object_name": "食物", "summary": f"{name}已经照顾孩子吃了东西。"}
        if any(marker in text for marker in ("抱了抱", "抱抱你", "拍了拍")):
            return {"kind": "comfort", "status": "completed", "object_name": "拥抱和陪伴", "summary": f"{name}刚刚抱抱并安抚了孩子。"}
        return None

    async def interact(
        self,
        *,
        actor: ActorContext,
        child_id: str,
        operation_id: str,
        idempotency_key: str,
        expected_state_version: int | None,
        message: str,
        source: SourceReference,
    ) -> dict[str, Any]:
        command = NurseryCommand(
            actor=actor,
            child_id=str(child_id).strip(),
            action=NurseryAction.CHILD_INTERACT,
            operation_id=str(operation_id).strip(),
            idempotency_key=str(idempotency_key).strip(),
            expected_state_version=expected_state_version,
            payload={},
            private_payload={"message": self._message(message)},
            source=source,
        )
        operation = await self.coordinator.submit(command)
        result = operation.as_dict()
        event = await asyncio.to_thread(self.store.get_short_event_by_operation, operation.operation_id)
        if event is not None:
            care = await asyncio.to_thread(
                self.store.get_shared_care_event_by_operation, operation.operation_id
            )
            result["interaction"] = {
                "event_id": event["event_id"],
                "reply": event["child_reply"],
                "intent": event["intent"],
                "conversation_channel": event["conversation_channel"],
                "expires_at": event["expires_at"],
                "care": care,
            }
        elif operation.status.value == "completed":
            result["interaction"] = {"retention": "expired"}
        return result

    async def _prepare(
        self, command: NurseryCommand, before: dict[str, Any]
    ) -> PreparedMutation:
        message = self._message((command.private_payload or {}).get("message"))
        config = await asyncio.to_thread(
            self.store.get_model_config_internal, command.actor.account_id
        )
        if not config or config.get("connection_status") != "ready":
            raise NurseryError("MODEL_NOT_CONFIGURED", "a ready child response model is required")
        credential = await asyncio.to_thread(self.vault.get, str(config["credential_ref"]))
        request = validate_connection_request(
            ModelConnectionRequest(
                provider_id=str(config["provider_id"]),
                base_url=str(config["base_url"]),
                model_name=str(config["model_name"]),
                credential=credential,
            )
        )
        settlement = await asyncio.to_thread(
            self.store.preview_body_settlement,
            child_id=command.child_id,
            through=self.store.clock(),
            rule=self.body_time_rule,
        )
        runtime_state = dict(settlement["runtime_state"])
        channel = self._conversation_channel(command.actor)
        caregiver = await asyncio.to_thread(
            self.store.get_caregiver_identity,
            actor=command.actor,
            child_id=command.child_id,
        )
        anima_contexts = await asyncio.to_thread(
            self.store.list_active_child_safe_anima_contexts, command.child_id
        )
        recent_conversation = await asyncio.to_thread(
            self.store.list_recent_child_interactions,
            command.child_id,
            conversation_channel=channel,
            caregiver_id=command.actor.caregiver_id,
            limit=8,
        )
        adapter = self.adapter_registry.get(request.provider_id)
        care_view = await asyncio.to_thread(
            self.store.get_child_feature_view,
            actor=command.actor,
            child_id=command.child_id,
            detail="care",
        )
        shared_care = [
            {key: item[key] for key in (
                "care_event_id", "category", "object_name", "summary",
                "caregiver_id", "caregiver_display_name", "created_at",
                "correction",
            ) if key in item}
            for item in care_view.get("care_view", {}).get("shared_care", [])[-8:]
        ]
        care = care_view.get("care_view", {})
        # Share factual handoff, never the other caregiver's private chat.
        shared_health_care = {
            "active_events": [
                {key: item[key] for key in ("health_event_id", "status", "summary", "updated_at") if key in item}
                for item in care.get("active_events", [])[:8]
            ],
            "confirmed_plans": [
                {key: item[key] for key in ("care_plan_id", "health_event_id", "plan_version", "summary", "status") if key in item}
                for item in care.get("confirmed_plans", [])[:8]
            ],
            "executions": [
                {key: item[key] for key in ("execution_id", "care_plan_id", "care_plan_version",
                    "health_event_id", "executed_at", "executed_by", "executed_by_display_name") if key in item}
                for item in care.get("executions", [])[:8]
            ],
        }
        family = await asyncio.to_thread(
            self.store.get_child_feature_view, actor=command.actor,
            child_id=command.child_id, detail="family",
        )
        family_view = family.get("family_view", {})
        family_context = {
            "threads": [
                {key: item[key] for key in ("thread_id", "kind", "title", "summary",
                 "status", "version", "updated_by_name", "current_session_over") if key in item}
                for item in family_view.get("threads", [])[:8]
            ],
            "my_relationship": family_view.get("my_relationship"),
            "recent_events": family_view.get("events", [])[:8],
        }
        conversation = ChildConversationRequest(
            message=message,
            stage_id=str(before["stage_id"]),
            runtime_state=runtime_state,
            anima_contexts=anima_contexts,
            recent_conversation=recent_conversation,
            actor_display_name=str(caregiver["display_name"]),
            actor_role=command.actor.role.value,
            conversation_channel=channel,
            shared_care=shared_care,
            shared_health_care=shared_health_care,
            family_context=family_context,
        )
        proposal = await asyncio.to_thread(
            adapter.respond,
            request,
            conversation,
        )
        try:
            checked = self._validate_proposal(runtime_state, proposal)
        except NurseryError as error:
            if error.code != "INVALID_CHILD_MODEL_OUTPUT":
                raise
            reply = " ".join(str(proposal.reply or "").split())
            if not reply or len(reply) > 500:
                raise
            # Conversation is independent from background extraction. If the
            # automatic state/care capture is invalid, keep the spoken reply
            # and discard only the malformed metadata.
            checked = {
                "reply": reply,
                "intent": "talk",
                "runtime_state": dict(runtime_state),
                "care_action": {
                    "kind": "none",
                    "status": "none",
                    "object_name": "",
                    "summary": "",
                },
            }
        if checked["care_action"].get("status") != "completed":
            recovered_care = self._clear_completed_care_from_message(
                message, str(caregiver.get("display_name") or "养育者")
            )
            if recovered_care is not None:
                checked["care_action"] = recovered_care
        expires_at = iso_time(self.store.clock() + timedelta(days=SHORT_EVENT_RETENTION_DAYS))

        async def evaluate_health_after_commit(_operation: dict[str, Any]) -> None:
            # The source is the completed interaction operation, never its
            # message or model output.  This is awaited, not queued or retried.
            await asyncio.to_thread(
                self.health_runtime.evaluate_interaction,
                account_id=command.actor.account_id,
                child_id=command.child_id,
                source=command.source,
                operation_id=command.operation_id,
            )

        return PreparedMutation(
            data={
                "event_id": f"interaction:{command.operation_id}",
                "runtime_state": checked["runtime_state"],
                "caregiver_message": message,
                "child_reply": checked["reply"],
                "intent": checked["intent"],
                "conversation_channel": channel,
                "care_action": checked["care_action"],
                "expires_at": expires_at,
                "body_settlement": settlement,
            },
            on_commit=evaluate_health_after_commit,
        )
