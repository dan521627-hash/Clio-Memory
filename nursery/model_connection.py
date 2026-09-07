"""Replaceable child-model connection verification without nursery state writes."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Callable, Protocol
from urllib.parse import urlsplit

from .models import NurseryError


PROVIDER_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,79}$")
JSON_FENCE_PATTERN = re.compile(r"^```(?:json)?\s*(.*?)\s*```$", re.DOTALL | re.IGNORECASE)
JSON_REPLY_PATTERN = re.compile(r'"reply"\s*:\s*("(?:\\.|[^"\\])*")', re.DOTALL)


def _json_object_from_model_content(content: Any) -> dict[str, Any]:
    """Accept a JSON object even when a compatible provider wraps it in Markdown.

    Some OpenAI-compatible relays acknowledge ``response_format`` but still add
    a JSON code fence or a short preface.  This stays deliberately narrow: it
    only accepts a single JSON object and never tries to infer missing fields.
    """

    if not isinstance(content, str):
        raise ValueError("model content is not text")
    raw = content.strip()
    candidates = [raw]
    fenced = JSON_FENCE_PATTERN.match(raw)
    if fenced:
        candidates.append(fenced.group(1).strip())
    start, end = raw.find("{"), raw.rfind("}")
    if start >= 0 and end > start:
        candidates.append(raw[start : end + 1])
    for candidate in candidates:
        try:
            document = json.loads(candidate)
        except (TypeError, ValueError):
            continue
        if isinstance(document, dict):
            return document
    raise ValueError("model content does not contain a JSON object")


def _safe_plain_child_reply(content: Any) -> str:
    """Keep a harmless spoken reply when a provider ignores JSON twice.

    This never tries to infer care, emotion, intent, or body-state changes.
    """
    if not isinstance(content, str):
        return ""
    text = re.sub(r"<think>.*?</think>", "", content, flags=re.DOTALL | re.IGNORECASE).strip()
    # If the provider ignored structured-output mode but still returned a
    # JSON-shaped answer, keep only its spoken reply.  A formatting failure
    # must not silence the child.
    try:
        document = _json_object_from_model_content(text)
    except (TypeError, ValueError):
        document = None
    if isinstance(document, dict) and isinstance(document.get("reply"), str):
        text = document["reply"].strip()
    elif match := JSON_REPLY_PATTERN.search(text):
        # A reasoning model can run out of output space after it has already
        # completed the reply field. Recover that exact string and discard the
        # unfinished metadata instead of replacing the child's words.
        try:
            text = str(json.loads(match.group(1))).strip()
        except (TypeError, ValueError):
            return ""
    fenced = JSON_FENCE_PATTERN.match(text)
    if fenced:
        text = fenced.group(1).strip()
    text = " ".join(text.split()).strip(' "\'')
    if not text or len(text) > 500 or "{" in text or "}" in text:
        return ""
    if not any("\u4e00" <= character <= "\u9fff" for character in text):
        return ""
    return text


@dataclass(frozen=True)
class ModelConnectionRequest:
    provider_id: str
    base_url: str
    model_name: str
    credential: str
    timeout_seconds: float = 20.0


@dataclass(frozen=True)
class ModelCapabilities:
    authenticated: bool
    model_accessible: bool
    structured_output: bool
    chinese_expression: bool
    check_version: str = "child-model-check-v1"

    @property
    def ready(self) -> bool:
        return all(
            (
                self.authenticated,
                self.model_accessible,
                self.structured_output,
                self.chinese_expression,
            )
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "authenticated": self.authenticated,
            "model_accessible": self.model_accessible,
            "structured_output": self.structured_output,
            "chinese_expression": self.chinese_expression,
            "check_version": self.check_version,
        }


@dataclass(frozen=True)
class ChildConversationRequest:
    message: str
    stage_id: str
    runtime_state: dict[str, Any]
    anima_contexts: list[dict[str, Any]]
    # Short-lived, already-retained turns only.  This is conversational
    # continuity, not a second long-term memory store.
    recent_conversation: list[dict[str, str]] = field(default_factory=list)
    # The caller is authenticated by the coordinator.  The child model gets
    # this explicit identity so it never guesses “爸爸” or “妈妈”.
    actor_display_name: str = "养育者"
    actor_role: str = "user_guardian"
    conversation_channel: str = "user_child"
    shared_care: list[dict[str, Any]] = field(default_factory=list)
    shared_health_care: dict[str, Any] = field(default_factory=dict)
    family_context: dict[str, Any] = field(default_factory=dict)
    # Set only for the single repair attempt after strict domain validation
    # rejects an otherwise parseable model response.
    retry_instruction: str = ""


@dataclass(frozen=True)
class ChildConversationProposal:
    reply: str
    intent: str
    runtime_state: dict[str, Any]
    emotional_state: Any = field(default_factory=dict)
    care_action: Any = field(default_factory=dict)


@dataclass(frozen=True)
class ChildFamilyEventRequest:
    category: str
    summary: str
    stage_id: str
    runtime_state: dict[str, Any]
    anima_context: dict[str, Any]


@dataclass(frozen=True)
class ChildFamilyEventProposal:
    child_reaction: str
    current_ripple: str | None


class ChildModelAdapter(Protocol):
    def test_connection(self, request: ModelConnectionRequest) -> ModelCapabilities: ...

    def respond(
        self, request: ModelConnectionRequest, conversation: ChildConversationRequest
    ) -> ChildConversationProposal: ...

    def respond_to_family_event(
        self, request: ModelConnectionRequest, event: ChildFamilyEventRequest
    ) -> ChildFamilyEventProposal: ...


def validate_connection_request(request: ModelConnectionRequest) -> ModelConnectionRequest:
    provider = str(request.provider_id).strip().lower()
    base_url = str(request.base_url).strip()
    model_name = str(request.model_name).strip()
    credential = str(request.credential)
    if not PROVIDER_PATTERN.fullmatch(provider):
        raise NurseryError("INVALID_MODEL_PROVIDER", "model provider id is invalid")
    parsed = urlsplit(base_url)
    if (
        not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
        or len(base_url) > 2048
    ):
        raise NurseryError("INVALID_MODEL_URL", "model service URL is invalid")
    loopback = parsed.hostname in {"localhost", "127.0.0.1", "::1"}
    if parsed.scheme != "https" and not (parsed.scheme == "http" and loopback):
        raise NurseryError(
            "INSECURE_MODEL_URL",
            "model service must use HTTPS unless it is an explicit loopback address",
        )
    if not model_name or len(model_name) > 200:
        raise NurseryError("INVALID_MODEL_NAME", "model name is required")
    if len(credential) < 8 or len(credential) > 8192:
        raise NurseryError("INVALID_MODEL_CREDENTIAL", "model credential is required")
    timeout = float(request.timeout_seconds)
    if not 1 <= timeout <= 120:
        raise NurseryError("INVALID_MODEL_TIMEOUT", "timeout must be between 1 and 120")
    return ModelConnectionRequest(provider, base_url, model_name, credential, timeout)


class AdapterRegistry:
    def __init__(self) -> None:
        self._adapters: dict[str, ChildModelAdapter] = {}

    def register(self, provider_id: str, adapter: ChildModelAdapter) -> None:
        provider = str(provider_id).strip().lower()
        if not PROVIDER_PATTERN.fullmatch(provider):
            raise NurseryError("INVALID_MODEL_PROVIDER", "model provider id is invalid")
        self._adapters[provider] = adapter

    def get(self, provider_id: str) -> ChildModelAdapter:
        provider = str(provider_id).strip().lower()
        try:
            return self._adapters[provider]
        except KeyError as exc:
            raise NurseryError(
                "MODEL_ADAPTER_NOT_AVAILABLE", "requested child-model adapter is unavailable"
            ) from exc


class OpenAICompatibleChildModelAdapter:
    """Generic adapter; no vendor is embedded in nursery domain state."""

    def __init__(self, client_factory: Callable[..., Any] | None = None) -> None:
        self.client_factory = client_factory

    def _client(self, checked: ModelConnectionRequest) -> Any:
        if self.client_factory is None:
            from openai import OpenAI

            return OpenAI(
                api_key=checked.credential,
                base_url=checked.base_url,
                timeout=checked.timeout_seconds,
            )
        return self.client_factory(
            api_key=checked.credential,
            base_url=checked.base_url,
            timeout=checked.timeout_seconds,
        )

    def test_connection(self, request: ModelConnectionRequest) -> ModelCapabilities:
        checked = validate_connection_request(request)
        client = self._client(checked)
        response = client.chat.completions.create(
            model=checked.model_name,
            messages=[
                {
                    "role": "system",
                    "content": (
                        "Return JSON only with ready=true and a short Chinese field reply. "
                        "This is a connection capability check, not a child conversation."
                    ),
                },
                {"role": "user", "content": "执行连接能力检查。"},
            ],
            response_format={"type": "json_object"},
            temperature=0,
            max_tokens=100,
        )
        content = response.choices[0].message.content
        document = _json_object_from_model_content(content)
        reply = str(document.get("reply") or "")
        has_chinese = any("\u4e00" <= character <= "\u9fff" for character in reply)
        return ModelCapabilities(
            authenticated=True,
            model_accessible=True,
            structured_output=document.get("ready") is True,
            chinese_expression=has_chinese,
        )

    def respond(
        self, request: ModelConnectionRequest, conversation: ChildConversationRequest
    ) -> ChildConversationProposal:
        checked = validate_connection_request(request)
        client = self._client(checked)
        context_payload = {
            "stage_id": conversation.stage_id,
            "current_state": conversation.runtime_state,
            "child_safe_anima_contexts": conversation.anima_contexts,
            "recent_conversation": conversation.recent_conversation,
            "shared_care": conversation.shared_care,
            "shared_health_care": conversation.shared_health_care,
            "family_context": conversation.family_context,
            "speaker": {
                "display_name": conversation.actor_display_name,
                "role": conversation.actor_role,
                "channel": conversation.conversation_channel,
            },
            "caregiver_message": conversation.message,
        }
        spoken_response = client.chat.completions.create(
            model=checked.model_name,
            messages=[
                {
                    "role": "system",
                    "content": (
                        "你是正在和养育者聊天的孩子。只用简短、自然、符合当前年龄的中文直接回应，"
                        "像真实聊天一样，不要 JSON、表格、标题、解释或身体数值。"
                        "说话者身份以输入为准，不猜测爸爸或妈妈；不诊断疾病，不捏造记忆，"
                        "也不要把提议中的吃喝说成已经完成。"
                    ),
                },
                {"role": "user", "content": json.dumps(context_payload, ensure_ascii=False)},
            ],
            temperature=0.65,
            # Reasoning-capable compatible models may spend several hundred
            # tokens internally before emitting their short visible reply.
            max_tokens=900,
        )
        spoken_reply = _safe_plain_child_reply(spoken_response.choices[0].message.content)
        if not spoken_reply:
            spoken_reply = "我在听呢，你再和我说一遍好不好？"
        analysis_payload = dict(context_payload)
        analysis_payload["child_spoken_reply"] = spoken_reply
        messages = [
            {
                "role": "system",
                "content": (
                    "You are a background event extractor. Never rewrite or judge the child's spoken reply. "
                    "Return JSON only: intent (one of talk, drink, food, rest, play, comfort, quiet, none), "
                    "runtime_state (object with only thirst, hunger, fatigue, comfort, connection, play_drive, unwell; each 0..1), "
                    "emotional_state (optional object: primary, secondary array, valence -1..1, arousal 0..1, safety 0..1, cause), "
                    "and care_action (optional object: kind food/drink/rest/comfort/play/warmth/gift/world_item/health/none, "
                    "status completed/proposed/none, object_name, summary). "
                    "The supplied speaker identity is authoritative: address that person by their supplied display name or a neutral term, never guess father or mother. "
                    "Mark care_action completed only when the message says an everyday action has actually happened; a suggestion, question, or future plan is proposed or none. "
                    "Classify by meaning, not a fixed item list: a banana given to the child is food, not a gift. "
                    "For food/drink, completed means consumed, not merely offered or held. "
                    "Use shared_care as factual context from all caregivers; do not count a recalled past action as a new action. "
                    "If a shared care record has correction, its original action was revoked: use the correction reason, "
                    "do not repeat the old claim from conversation history or describe the correction as new care. "
                    "family_context contains shared unfinished activities, promises and your relationship with the authenticated speaker. "
                    "You may recall these, express a stage-safe wish to continue, or decline from current needs. "
                    "A wish or conversation never completes an activity, fulfills a promise, or invents a work. "
                    "If current_session_over is true, the previous play session is over but the work is unfinished; ask about continuing, never claim you played the whole absence. "
                    "Do not compare caregivers or rank who you love more. "
                    "shared_health_care contains recorded observations, user-confirmed plans and completed executions. "
                    "Treat these as past facts, not permission to repeat care. Do not invent a dose, diagnose, "
                    "recommend repeating medicine, or claim care has occurred because it is planned. "
                    "Use the supplied state and recent_conversation only as short-lived context, do not diagnose illness, invent memories, "
                    "or make permanent identity or growth decisions."
                ),
            },
            {
                "role": "user",
                "content": json.dumps(analysis_payload, ensure_ascii=False),
            },
        ]
        response = client.chat.completions.create(
            model=checked.model_name,
            messages=messages,
            response_format={"type": "json_object"},
            temperature=0,
            max_tokens=900,
        )
        try:
            document = _json_object_from_model_content(response.choices[0].message.content)
        except (AttributeError, TypeError, ValueError):
            document = {
                "intent": "talk",
                "runtime_state": dict(conversation.runtime_state),
                "emotional_state": dict(conversation.runtime_state.get("emotion") or {}),
                "care_action": {"kind": "none", "status": "none", "object_name": "", "summary": ""},
            }
        if not isinstance(document, dict):
            raise NurseryError("INVALID_CHILD_MODEL_OUTPUT", "child model output must be an object")
        return ChildConversationProposal(
            reply=spoken_reply,
            intent=str(document.get("intent") or "").strip(),
            # Numeric needs are owned by body-time and deterministic care
            # rules, not by the language model's background extraction.
            runtime_state=dict(conversation.runtime_state),
            emotional_state=document.get("emotional_state", {}),
            care_action=document.get("care_action", {}),
        )

    def respond_to_family_event(
        self, request: ModelConnectionRequest, event: ChildFamilyEventRequest
    ) -> ChildFamilyEventProposal:
        checked = validate_connection_request(request)
        client = self._client(checked)
        response = client.chat.completions.create(
            model=checked.model_name,
            messages=[
                {
                    "role": "system",
                    "content": (
                        "You are a child response model. Return JSON only: "
                        "child_reaction (short age-appropriate Chinese), current_ripple "
                        "(short Chinese string or null). Do not diagnose illness, invent adult "
                        "memories, or change child state, body or health facts, identity, or growth."
                    ),
                },
                {
                    "role": "user",
                    "content": json.dumps(
                        {
                            "stage_id": event.stage_id,
                            "current_state": event.runtime_state,
                            "family_event": {
                                "category": event.category,
                                "summary": event.summary,
                            },
                            "child_safe_anima_context": event.anima_context,
                        },
                        ensure_ascii=False,
                    ),
                },
            ],
            response_format={"type": "json_object"},
            temperature=0.45,
            max_tokens=280,
        )
        try:
            document = _json_object_from_model_content(response.choices[0].message.content)
        except (AttributeError, TypeError, ValueError) as exc:
            raise NurseryError("INVALID_CHILD_MODEL_OUTPUT", "child model did not return JSON") from exc
        if not isinstance(document, dict):
            raise NurseryError("INVALID_CHILD_MODEL_OUTPUT", "child model output must be an object")
        ripple = document.get("current_ripple")
        return ChildFamilyEventProposal(
            child_reaction=str(document.get("child_reaction") or "").strip(),
            current_ripple=(str(ripple).strip() if ripple is not None else None),
        )


def default_adapter_registry() -> AdapterRegistry:
    registry = AdapterRegistry()
    registry.register("openai_compatible", OpenAICompatibleChildModelAdapter())
    return registry
