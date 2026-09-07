"""Private HTTP adapter for Anima's stage-2 nursery creation service.

The browser-facing manager may proxy these routes, but only ombre-brain owns
the coordinator, encrypted vault, and nursery.sqlite3 writes.
"""

from __future__ import annotations

import asyncio
import hmac
import json
import os
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from starlette.requests import Request
from starlette.responses import JSONResponse

from .capability_rules import load_capability_rules
from .body_state import BodyTimeRule, nursery_body_time_rule
from .anima_bridge import AnimaNurseryBridge
from .creation_queries import NurseryCreationQueryService
from .creation_service import NurseryCreationService
from .interaction_service import NurseryInteractionService
from .feature_service import NurseryFeatureService
from .models import (
    ActorContext,
    AuthSource,
    CaregiverRole,
    NurseryAction,
    NurseryCommand,
    NurseryError,
    SourceReference,
    SourceType,
)
from .privacy import public_status_view
from .secret_vault import FernetFileSecretVault
from .store import NurseryStore


MAX_JSON_BODY_BYTES = 64 * 1024
INTERNAL_TOKEN_ENV = "OMBRE_NURSERY_INTERNAL_TOKEN"
VAULT_KEY_ENV = "OMBRE_NURSERY_VAULT_KEY"
RULES_PATH_ENV = "OMBRE_NURSERY_RULES_PATH"
DB_PATH_ENV = "OMBRE_NURSERY_DB"
VAULT_DIR_ENV = "OMBRE_NURSERY_VAULT_DIR"

USER_STAGE2_ACTIONS = frozenset(
    {
        NurseryAction.BIND_EXTERNAL_GUARDIAN,
        NurseryAction.SAVE_MODEL_CONNECTION,
        NurseryAction.RETEST_MODEL_CONNECTION,
        NurseryAction.DELETE_MODEL_CONNECTION,
        NurseryAction.SAVE_DRAFT_IDENTITY,
        NurseryAction.SAVE_NAME_PROPOSALS,
        NurseryAction.REVIEW_NAME_CANDIDATES,
        NurseryAction.SELECT_DRAFT_NAME,
        NurseryAction.SUBMIT_TEMPERAMENT_QUESTIONNAIRE,
        NurseryAction.SUBMIT_INITIAL_STYLE,
        NurseryAction.SAVE_INITIAL_SPACE,
        NurseryAction.CONFIRM_CREATION,
    }
)


def _domain_status(error: NurseryError) -> int:
    if error.code in {
        "CAREGIVER_NOT_REGISTERED",
        "DRAFT_NOT_FOUND",
        "NURSERY_NOT_FOUND",
        "OPERATION_NOT_FOUND",
    }:
        return 404
    if error.code in {
        "ACTOR_NOT_GUARDIAN",
        "ACTOR_SCOPE_MISMATCH",
        "CREATION_DRAFT_FORBIDDEN",
        "PERMISSION_DENIED",
        "USER_GUARDIAN_REQUIRED",
    }:
        return 403
    if error.code in {
        "CONFIRMATION_VERSION_CONFLICT",
        "CREATION_NOT_READY",
        "IDEMPOTENCY_KEY_CONFLICT",
        "INVALID_STATE_TRANSITION",
        "MULTIPLE_CHILDREN_NOT_SUPPORTED",
        "OPERATION_ID_CONFLICT",
        "STATE_VERSION_CONFLICT",
    }:
        return 409
    if error.code.endswith("NOT_CONFIGURED") or error.code in {
        "INVALID_VAULT_KEY",
        "NURSERY_SERVICE_NOT_CONFIGURED",
    }:
        return 503
    return 422


def _error_response(error: NurseryError) -> JSONResponse:
    return JSONResponse(
        {"ok": False, "error": error.as_dict()},
        status_code=_domain_status(error),
    )


def _unexpected_response() -> JSONResponse:
    return JSONResponse(
        {
            "ok": False,
            "error": {
                "code": "RETRYABLE_FAILURE",
                "message": "nursery request failed without changing the saved state",
            },
        },
        status_code=503,
    )


async def _read_json_object(request: Request) -> dict[str, Any]:
    declared = request.headers.get("content-length", "").strip()
    if declared:
        try:
            if int(declared) > MAX_JSON_BODY_BYTES:
                raise NurseryError("REQUEST_TOO_LARGE", "request body is too large")
        except ValueError as exc:
            raise NurseryError("INVALID_CONTENT_LENGTH", "content length is invalid") from exc
    raw = await request.body()
    if len(raw) > MAX_JSON_BODY_BYTES:
        raise NurseryError("REQUEST_TOO_LARGE", "request body is too large")
    try:
        payload = json.loads(raw.decode("utf-8")) if raw else {}
    except (UnicodeDecodeError, ValueError) as exc:
        raise NurseryError("INVALID_JSON", "request body must be a JSON object") from exc
    if not isinstance(payload, dict):
        raise NurseryError("INVALID_JSON", "request body must be a JSON object")
    return payload


def _reject_unknown_fields(payload: Mapping[str, Any], allowed: set[str]) -> None:
    unknown = sorted(str(key) for key in payload if str(key) not in allowed)
    if unknown:
        raise NurseryError(
            "UNKNOWN_REQUEST_FIELD",
            "request contains unsupported fields",
            details={"fields": unknown},
        )


_FAMILY_EVENT_FIELDS = {
    "source_key",
    "source_version",
    "category",
    "child_safe_summary",
    "occurred_at",
}


def _family_source_type(source_key: object) -> SourceType:
    """Infer a source type from the only two safe source namespaces."""

    value = str(source_key or "").strip()
    if value.startswith("mailbox:") and value.removeprefix("mailbox:").strip():
        return SourceType.MAILBOX_EVENT
    if value.startswith("memory:") and value.removeprefix("memory:").strip():
        return SourceType.ANIMA_MEMORY
    raise NurseryError(
        "INVALID_ANIMA_FAMILY_SOURCE",
        "family events require a mailbox: or memory: source key",
    )


def _family_event_envelope(value: object) -> tuple[dict[str, Any], SourceType]:
    if not isinstance(value, dict):
        raise NurseryError("INVALID_JSON", "family event must be a JSON object")
    _reject_unknown_fields(value, _FAMILY_EVENT_FIELDS)
    source_type = _family_source_type(value.get("source_key"))
    # Health relevance is intentionally not request-controlled.  Until a
    # trusted local classifier produces it, every cross-service event is false.
    return ({key: value.get(key) for key in _FAMILY_EVENT_FIELDS}, source_type)


@dataclass(frozen=True)
class NurseryHTTPRuntime:
    store: NurseryStore
    creation: NurseryCreationService
    drafts: NurseryCreationQueryService
    interaction: NurseryInteractionService
    user_actor: ActorContext
    feature: NurseryFeatureService | None = None
    anima_bridge: AnimaNurseryBridge | None = None
    body_time_rule: BodyTimeRule | None = None

    def _anima_child_id(self, *, active_only: bool) -> str | None:
        """Resolve the one owned child locally; callers never supply it."""

        overview = self.store.get_account_overview(self.user_actor.account_id)
        child_id = str(overview.get("child_id") or "").strip()
        if not child_id:
            return None
        if active_only and overview.get("module_state") != "active":
            return None
        return child_id

    def _bridge(self) -> AnimaNurseryBridge:
        return self.anima_bridge or AnimaNurseryBridge(
            self.store,
            vault=self.creation.vault,
            adapter_registry=self.creation.adapter_registry,
        )

    async def apply_anima_family_event(
        self, child_safe_event: dict[str, Any], source_type: SourceType
    ) -> dict[str, Any]:
        """Apply one vetted event to this account's active child only."""

        child_id = self._anima_child_id(active_only=True)
        if not child_id:
            return {"family_event_ignored": "nursery_not_active"}
        return await self._bridge().apply_family_event(
            child_id=child_id,
            child_safe_event=child_safe_event,
            # The caller cannot inject Anima's disposition report.  A later
            # local-only provider may enrich this with the same safe contract.
            disposition_report={},
            source_type=source_type,
        )

    def supersede_anima_source(
        self,
        *,
        source_type: SourceType,
        source_key: str,
        source_version: str,
        superseded_by_source_version: str,
    ) -> dict[str, Any]:
        child_id = self._anima_child_id(active_only=False)
        if not child_id:
            return {"source_ignored": "nursery_not_created"}
        return self.store.supersede_source(
            source_type=source_type.value,
            source_id=source_key,
            source_version=source_version,
            superseded_by_source_version=superseded_by_source_version,
            account_id=self.user_actor.account_id,
            child_id=child_id,
        )

    def revoke_anima_source(
        self,
        *,
        source_type: SourceType,
        source_key: str,
        source_version: str,
    ) -> dict[str, Any]:
        child_id = self._anima_child_id(active_only=False)
        if not child_id:
            return {"source_ignored": "nursery_not_created"}
        return self.store.mark_source_revoked(
            source_type=source_type.value,
            source_id=source_key,
            source_version=source_version,
            account_id=self.user_actor.account_id,
            child_id=child_id,
        )

    def status(self) -> dict[str, Any]:
        overview = self.store.get_account_overview(self.user_actor.account_id)
        # Opening the room is read-only.  The phone receives only the saved
        # short-lived child state, never a raw event, adult memory, or a model call.
        if overview["module_state"] in {"active", "paused"} and overview["child_id"]:
            overview["child_state"] = self.store.get_child_runtime_state(
                str(overview["child_id"])
            )
            identity = self.store.get_child_identity(str(overview["child_id"])) or {}
            overview["child_name"] = str(
                identity.get("nickname") or identity.get("official_name") or "孩子"
            )
            overview["sex_status"] = str(identity.get("sex_status") or "neutral")
        return public_status_view(overview)

    def entry_preference(self) -> dict[str, Any]:
        return self.store.get_entry_preference(self.user_actor.account_id)

    def update_entry_preference(self, preference: str) -> dict[str, Any]:
        return self.store.set_entry_preference(self.user_actor, preference)

    def draft(self, child_id: str) -> dict[str, Any]:
        return self.drafts.get_draft(actor=self.user_actor, child_id=child_id)

    def operation(self, operation_id: str) -> dict[str, Any]:
        operation = self.store.get_operation_for_account(
            self.user_actor.account_id, operation_id
        )
        if operation is None:
            raise NurseryError("OPERATION_NOT_FOUND", "operation was not found")
        from .coordinator import NurseryCoordinator

        return NurseryCoordinator._from_operation(operation, replayed=True).as_dict()

    def child_status(
        self,
        child_id: str,
        *,
        detail: str = "summary",
        since: str | None = None,
        query: str = "",
        cursor: str | None = None,
    ) -> dict[str, Any]:
        from .queries import NurseryQueryService

        return NurseryQueryService(
            self.store, body_time_rule=self.body_time_rule,
            capability_rules=self.creation.capability_rules if self.creation is not None else None,
        ).get_child_status(
            actor=self.user_actor,
            child_id=str(child_id),
            detail=detail,
            since=since,
            query=query,
            cursor=cursor,
        )


class NurseryRuntimeProvider:
    """Lazily builds production components only after internal authentication."""

    def __init__(
        self,
        config: Mapping[str, Any],
        *,
        environment: Mapping[str, str] | None = None,
    ) -> None:
        self.config = dict(config)
        self.environment = environment if environment is not None else os.environ
        self._runtime: NurseryHTTPRuntime | None = None
        self._guard = threading.Lock()

    def _required(self, name: str) -> str:
        value = str(self.environment.get(name, "")).strip()
        if not value:
            raise NurseryError(
                "NURSERY_SERVICE_NOT_CONFIGURED",
                f"required nursery setting is missing: {name}",
            )
        return value

    def authorize(self, supplied: str) -> None:
        expected = str(self.environment.get(INTERNAL_TOKEN_ENV, "")).strip()
        if len(expected) < 32:
            raise NurseryError(
                "NURSERY_SERVICE_NOT_CONFIGURED",
                "nursery internal authentication is not configured",
            )
        if not supplied or not hmac.compare_digest(expected, str(supplied)):
            raise NurseryError("INTERNAL_AUTH_REQUIRED", "internal authentication failed")

    def get(self) -> NurseryHTTPRuntime:
        with self._guard:
            if self._runtime is not None:
                return self._runtime
            try:
                data_root = Path(
                    str(self.config.get("buckets_dir") or "buckets")
                ).resolve()
                db_path = str(
                    self.environment.get(DB_PATH_ENV, "")
                    or (data_root / "nursery.sqlite3")
                )
                vault_root = str(
                    self.environment.get(VAULT_DIR_ENV, "")
                    or (data_root / "nursery-secrets")
                )
                rules = load_capability_rules(self._required(RULES_PATH_ENV))
                vault = FernetFileSecretVault(
                    vault_root, self._required(VAULT_KEY_ENV)
                )
                account_id = str(
                    self.environment.get("OMBRE_NURSERY_ACCOUNT_ID", "anima-owner")
                ).strip()
                caregiver_id = str(
                    self.environment.get(
                        "OMBRE_NURSERY_USER_CAREGIVER_ID", "anima-user-guardian"
                    )
                ).strip()
                actor = ActorContext.build(
                    account_id=account_id,
                    caregiver_id=caregiver_id,
                    role=CaregiverRole.USER_GUARDIAN,
                    permissions=("nursery.*",),
                    auth_source=AuthSource.MANAGER_SESSION,
                )
                store = NurseryStore(db_path)
                body_time_rule = nursery_body_time_rule()
                creation = NurseryCreationService(
                    store,
                    vault=vault,
                    capability_rules=rules,
                    body_time_rule=body_time_rule,
                )
                self._runtime = NurseryHTTPRuntime(
                    store=store,
                    creation=creation,
                    drafts=NurseryCreationQueryService(store),
                    interaction=NurseryInteractionService(
                        store, vault=vault, body_time_rule=body_time_rule
                    ),
                    feature=NurseryFeatureService(
                        store, body_time_rule=body_time_rule, capability_rules=creation.capability_rules
                    ),
                    user_actor=actor,
                    body_time_rule=body_time_rule,
                    anima_bridge=AnimaNurseryBridge(
                        store,
                        vault=creation.vault,
                        adapter_registry=creation.adapter_registry,
                    ),
                )
                return self._runtime
            except NurseryError:
                raise
            except Exception as exc:
                raise NurseryError(
                    "NURSERY_SERVICE_NOT_CONFIGURED",
                    "nursery storage, rules, or secret vault could not be initialized",
                ) from exc


class NurseryInternalHTTPAPI:
    def __init__(self, provider: NurseryRuntimeProvider) -> None:
        self.provider = provider

    async def _runtime(self, request: Request) -> NurseryHTTPRuntime:
        supplied = request.headers.get("x-anima-internal-token", "")
        self.provider.authorize(supplied)
        return await asyncio.to_thread(self.provider.get)

    @staticmethod
    def _internal_error(error: NurseryError) -> JSONResponse:
        if error.code == "INTERNAL_AUTH_REQUIRED":
            return JSONResponse(
                {"ok": False, "error": {"code": error.code, "message": error.message}},
                status_code=401,
            )
        return _error_response(error)

    async def apply_anima_family_event(self, request: Request) -> JSONResponse:
        try:
            runtime = await self._runtime(request)
            body = await _read_json_object(request)
            _reject_unknown_fields(body, {"event"})
            event, source_type = _family_event_envelope(body.get("event"))
            result = await runtime.apply_anima_family_event(event, source_type)
            return JSONResponse({"ok": True, "result": result})
        except NurseryError as error:
            return self._internal_error(error)
        except Exception:
            return _unexpected_response()

    async def supersede_anima_family_event(self, request: Request) -> JSONResponse:
        try:
            runtime = await self._runtime(request)
            body = await _read_json_object(request)
            _reject_unknown_fields(
                body,
                {"source_key", "source_version", "superseded_by_source_version"},
            )
            source_type = _family_source_type(body.get("source_key"))
            result = await asyncio.to_thread(
                runtime.supersede_anima_source,
                source_type=source_type,
                source_key=str(body.get("source_key") or "").strip(),
                source_version=str(body.get("source_version") or "").strip(),
                superseded_by_source_version=str(
                    body.get("superseded_by_source_version") or ""
                ).strip(),
            )
            return JSONResponse({"ok": True, "result": result})
        except NurseryError as error:
            return self._internal_error(error)
        except Exception:
            return _unexpected_response()

    async def revoke_anima_family_event(self, request: Request) -> JSONResponse:
        try:
            runtime = await self._runtime(request)
            body = await _read_json_object(request)
            _reject_unknown_fields(body, {"source_key", "source_version"})
            source_type = _family_source_type(body.get("source_key"))
            result = await asyncio.to_thread(
                runtime.revoke_anima_source,
                source_type=source_type,
                source_key=str(body.get("source_key") or "").strip(),
                source_version=str(body.get("source_version") or "").strip(),
            )
            return JSONResponse({"ok": True, "result": result})
        except NurseryError as error:
            return self._internal_error(error)
        except Exception:
            return _unexpected_response()

    async def status(self, request: Request) -> JSONResponse:
        try:
            runtime = await self._runtime(request)
            result = await asyncio.to_thread(runtime.status)
            return JSONResponse({"ok": True, "status": result})
        except NurseryError as error:
            if error.code == "INTERNAL_AUTH_REQUIRED":
                return JSONResponse(
                    {"ok": False, "error": {"code": error.code, "message": error.message}},
                    status_code=401,
                )
            return _error_response(error)
        except Exception:
            return _unexpected_response()

    async def get_entry_preference(self, request: Request) -> JSONResponse:
        try:
            runtime = await self._runtime(request)
            result = await asyncio.to_thread(runtime.entry_preference)
            return JSONResponse({"ok": True, **result})
        except NurseryError as error:
            if error.code == "INTERNAL_AUTH_REQUIRED":
                return JSONResponse(
                    {"ok": False, "error": {"code": error.code, "message": error.message}},
                    status_code=401,
                )
            return _error_response(error)
        except Exception:
            return _unexpected_response()

    async def update_entry_preference(self, request: Request) -> JSONResponse:
        try:
            runtime = await self._runtime(request)
            body = await _read_json_object(request)
            _reject_unknown_fields(body, {"preference"})
            result = await asyncio.to_thread(
                runtime.update_entry_preference,
                str(body.get("preference") or ""),
            )
            return JSONResponse({"ok": True, **result})
        except NurseryError as error:
            if error.code == "INTERNAL_AUTH_REQUIRED":
                return JSONResponse(
                    {"ok": False, "error": {"code": error.code, "message": error.message}},
                    status_code=401,
                )
            return _error_response(error)
        except Exception:
            return _unexpected_response()

    async def start_draft(self, request: Request) -> JSONResponse:
        try:
            runtime = await self._runtime(request)
            body = await _read_json_object(request)
            _reject_unknown_fields(
                body,
                {"operation_id", "idempotency_key", "display_name"},
            )
            operation_id = str(body.get("operation_id") or "").strip()
            result = await runtime.creation.start_draft(
                actor=runtime.user_actor,
                operation_id=operation_id,
                idempotency_key=str(body.get("idempotency_key") or "").strip(),
                source=SourceReference.build(
                    source_type=SourceType.USER_INTERACTION,
                    source_id=f"manager:nursery:{operation_id}",
                    source_version="v1",
                    specification_ref="nursery-stage2-http-v1",
                ),
                display_name=str(body.get("display_name") or "").strip(),
            )
            return JSONResponse({"ok": True, "operation": result.as_dict()})
        except NurseryError as error:
            if error.code == "INTERNAL_AUTH_REQUIRED":
                return JSONResponse(
                    {"ok": False, "error": {"code": error.code, "message": error.message}},
                    status_code=401,
                )
            return _error_response(error)
        except Exception:
            return _unexpected_response()

    async def get_draft(self, request: Request) -> JSONResponse:
        try:
            runtime = await self._runtime(request)
            child_id = str(request.path_params.get("child_id") or "").strip()
            draft = await asyncio.to_thread(runtime.draft, child_id)
            return JSONResponse({"ok": True, "draft": draft})
        except NurseryError as error:
            if error.code == "INTERNAL_AUTH_REQUIRED":
                return JSONResponse(
                    {"ok": False, "error": {"code": error.code, "message": error.message}},
                    status_code=401,
                )
            return _error_response(error)
        except Exception:
            return _unexpected_response()

    async def submit_operation(self, request: Request) -> JSONResponse:
        try:
            runtime = await self._runtime(request)
            body = await _read_json_object(request)
            _reject_unknown_fields(
                body,
                {
                    "action",
                    "operation_id",
                    "idempotency_key",
                    "expected_state_version",
                    "payload",
                    "private",
                    "resume_retryable",
                },
            )
            try:
                action = NurseryAction(str(body.get("action") or ""))
            except ValueError as exc:
                raise NurseryError("UNKNOWN_ACTION", "nursery action is invalid") from exc
            if action not in USER_STAGE2_ACTIONS:
                raise NurseryError(
                    "ACTION_NOT_EXPOSED",
                    "this action is not available through the user creation adapter",
                )
            payload = body.get("payload") or {}
            private = body.get("private")
            if not isinstance(payload, dict):
                raise NurseryError("INVALID_PAYLOAD", "payload must be an object")
            if private is not None and not isinstance(private, dict):
                raise NurseryError("INVALID_PRIVATE_PAYLOAD", "private must be an object")
            operation_id = str(body.get("operation_id") or "").strip()
            expected = body.get("expected_state_version")
            command = NurseryCommand(
                actor=runtime.user_actor,
                child_id=str(request.path_params.get("child_id") or "").strip(),
                action=action,
                operation_id=operation_id,
                idempotency_key=str(body.get("idempotency_key") or "").strip(),
                expected_state_version=expected,
                payload=dict(payload),
                private_payload=dict(private) if private is not None else None,
                source=SourceReference.build(
                    source_type=SourceType.USER_INTERACTION,
                    source_id=f"manager:nursery:{operation_id}",
                    source_version="v1",
                    specification_ref="nursery-stage2-http-v1",
                ),
            )
            result = await runtime.creation.submit(
                command,
                resume_retryable=body.get("resume_retryable") is True,
            )
            return JSONResponse({"ok": True, "operation": result.as_dict()})
        except NurseryError as error:
            if error.code == "INTERNAL_AUTH_REQUIRED":
                return JSONResponse(
                    {"ok": False, "error": {"code": error.code, "message": error.message}},
                    status_code=401,
                )
            return _error_response(error)
        except Exception:
            return _unexpected_response()

    async def get_operation(self, request: Request) -> JSONResponse:
        try:
            runtime = await self._runtime(request)
            operation_id = str(request.path_params.get("operation_id") or "").strip()
            result = await asyncio.to_thread(runtime.operation, operation_id)
            return JSONResponse({"ok": True, "operation": result})
        except NurseryError as error:
            if error.code == "INTERNAL_AUTH_REQUIRED":
                return JSONResponse(
                    {"ok": False, "error": {"code": error.code, "message": error.message}},
                    status_code=401,
                )
            return _error_response(error)
        except Exception:
            return _unexpected_response()

    async def child_status(self, request: Request) -> JSONResponse:
        try:
            runtime = await self._runtime(request)
            child_id = str(request.path_params.get("child_id") or "").strip()
            result = await asyncio.to_thread(
                runtime.child_status,
                child_id,
                detail=request.query_params.get("detail", "summary"),
                since=request.query_params.get("since"),
                query=request.query_params.get("query", ""),
                cursor=request.query_params.get("cursor"),
            )
            return JSONResponse({"ok": True, "status": result})
        except NurseryError as error:
            if error.code == "INTERNAL_AUTH_REQUIRED":
                return JSONResponse(
                    {"ok": False, "error": {"code": error.code, "message": error.message}},
                    status_code=401,
                )
            return _error_response(error)
        except Exception:
            return _unexpected_response()

    async def submit_feature_operation(self, request: Request) -> JSONResponse:
        try:
            runtime = await self._runtime(request)
            if runtime.feature is None:
                raise NurseryError("NURSERY_SERVICE_NOT_CONFIGURED", "nursery feature service is not configured")
            body = await _read_json_object(request)
            _reject_unknown_fields(
                body,
                {
                    "action",
                    "operation_id",
                    "idempotency_key",
                    "expected_state_version",
                    "payload",
                    "resume_retryable",
                },
            )
            try:
                action = NurseryAction(str(body.get("action") or ""))
            except ValueError as exc:
                raise NurseryError("UNKNOWN_ACTION", "nursery action is invalid") from exc
            payload = body.get("payload") or {}
            if not isinstance(payload, dict):
                raise NurseryError("INVALID_PAYLOAD", "payload must be an object")
            result = await runtime.feature.submit(
                actor=runtime.user_actor,
                child_id=str(request.path_params.get("child_id") or "").strip(),
                action=action,
                payload=payload,
                operation_id=str(body.get("operation_id") or "").strip() or None,
                idempotency_key=str(body.get("idempotency_key") or "").strip() or None,
                expected_state_version=body.get("expected_state_version"),
                resume_retryable=body.get("resume_retryable") is True,
                source=SourceReference.build(
                    source_type=SourceType.USER_INTERACTION,
                    source_id=f"manager:nursery:feature:{body.get('operation_id') or ''}",
                    source_version="v1",
                    specification_ref="nursery-feature-http-v1",
                ),
            )
            return JSONResponse({"ok": True, "operation": result})
        except NurseryError as error:
            if error.code == "INTERNAL_AUTH_REQUIRED":
                return JSONResponse(
                    {"ok": False, "error": {"code": error.code, "message": error.message}},
                    status_code=401,
                )
            return _error_response(error)
        except Exception:
            return _unexpected_response()

    async def child_interact(self, request: Request) -> JSONResponse:
        try:
            runtime = await self._runtime(request)
            body = await _read_json_object(request)
            _reject_unknown_fields(
                body,
                {
                    "message",
                    "operation_id",
                    "idempotency_key",
                    "expected_state_version",
                },
            )
            operation_id = str(body.get("operation_id") or "").strip()
            result = await runtime.interaction.interact(
                actor=runtime.user_actor,
                child_id=str(request.path_params.get("child_id") or "").strip(),
                operation_id=operation_id,
                idempotency_key=str(body.get("idempotency_key") or "").strip(),
                expected_state_version=body.get("expected_state_version"),
                message=str(body.get("message") or ""),
                source=SourceReference.build(
                    source_type=SourceType.USER_INTERACTION,
                    source_id=f"manager:nursery:interaction:{operation_id}",
                    source_version="v1",
                    specification_ref="nursery-stage3-user-http-v1",
                ),
            )
            return JSONResponse({"ok": True, "operation": result})
        except NurseryError as error:
            if error.code == "INTERNAL_AUTH_REQUIRED":
                return JSONResponse(
                    {"ok": False, "error": {"code": error.code, "message": error.message}},
                    status_code=401,
                )
            return _error_response(error)
        except Exception:
            return _unexpected_response()

def register_nursery_internal_routes(
    mcp: Any, config: Mapping[str, Any]
) -> NurseryInternalHTTPAPI:
    """Register private routes without initializing storage or reading secrets."""

    api = NurseryInternalHTTPAPI(NurseryRuntimeProvider(config))
    mcp.custom_route(
        "/internal/nursery/anima/family-events", methods=["POST"]
    )(api.apply_anima_family_event)
    mcp.custom_route(
        "/internal/nursery/anima/family-events/supersede", methods=["POST"]
    )(api.supersede_anima_family_event)
    mcp.custom_route(
        "/internal/nursery/anima/family-events/revoke", methods=["POST"]
    )(api.revoke_anima_family_event)
    mcp.custom_route("/internal/nursery/user/status", methods=["GET"])(api.status)
    mcp.custom_route(
        "/internal/nursery/user/entry-preference", methods=["GET"]
    )(api.get_entry_preference)
    mcp.custom_route(
        "/internal/nursery/user/entry-preference", methods=["PUT"]
    )(api.update_entry_preference)
    mcp.custom_route("/internal/nursery/user/drafts", methods=["POST"])(
        api.start_draft
    )
    mcp.custom_route(
        "/internal/nursery/user/drafts/{child_id}", methods=["GET"]
    )(api.get_draft)
    mcp.custom_route(
        "/internal/nursery/user/drafts/{child_id}/operations", methods=["POST"]
    )(api.submit_operation)
    mcp.custom_route(
        "/internal/nursery/user/operations/{operation_id}", methods=["GET"]
    )(api.get_operation)
    mcp.custom_route(
        "/internal/nursery/user/children/{child_id}/interactions", methods=["POST"]
    )(api.child_interact)
    mcp.custom_route(
        "/internal/nursery/user/children/{child_id}/status", methods=["GET"]
    )(api.child_status)
    mcp.custom_route(
        "/internal/nursery/user/children/{child_id}/operations", methods=["POST"]
    )(api.submit_feature_operation)
    return api
