"""Domain types and state rules for the nursery module."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Iterable


class TextEnum(str, Enum):
    def __str__(self) -> str:
        return self.value


class ModuleState(TextEnum):
    NEVER_ENABLED = "never_enabled"
    DRAFT = "draft"
    ACTIVE = "active"
    PAUSED = "paused"
    DELETION_PENDING = "deletion_pending"


class CaregiverRole(TextEnum):
    USER_GUARDIAN = "user_guardian"
    EXTERNAL_AI_GUARDIAN = "external_ai_guardian"
    COMPANION = "companion"
    SYSTEM_EVENT = "system_event"


class SexStatus(TextEnum):
    BOY = "boy"
    GIRL = "girl"
    NEUTRAL = "neutral"
    UNDECIDED = "undecided"


class QuestionnaireKind(TextEnum):
    TEMPERAMENT = "temperament"
    INITIAL_STYLE = "initial_style"


class NamePreference(TextEnum):
    LIKE = "like"
    ACCEPTABLE = "acceptable"
    REJECT = "reject"


class AuthSource(TextEnum):
    MANAGER_SESSION = "manager_session"
    MCP_SESSION = "mcp_session"
    INTERNAL_EVENT = "internal_event"
    TEST_FIXTURE = "test_fixture"


class NurseryAction(TextEnum):
    START_DRAFT = "start_draft"
    ACTIVATE_FIXTURE = "activate_fixture"
    BIND_EXTERNAL_GUARDIAN = "bind_external_guardian"
    SAVE_MODEL_CONNECTION = "save_model_connection"
    RETEST_MODEL_CONNECTION = "retest_model_connection"
    DELETE_MODEL_CONNECTION = "delete_model_connection"
    SAVE_DRAFT_IDENTITY = "save_draft_identity"
    SAVE_NAME_PROPOSALS = "save_name_proposals"
    REVIEW_NAME_CANDIDATES = "review_name_candidates"
    SELECT_DRAFT_NAME = "select_draft_name"
    SUBMIT_TEMPERAMENT_QUESTIONNAIRE = "submit_temperament_questionnaire"
    SUBMIT_INITIAL_STYLE = "submit_initial_style"
    SAVE_INITIAL_SPACE = "save_initial_space"
    CONFIRM_CREATION = "confirm_creation"
    SYNC_ANIMA_CONTEXT = "sync_anima_context"
    APPLY_ANIMA_FAMILY_EVENT = "apply_anima_family_event"
    CHILD_INTERACT = "child_interact"
    ADD_AREA = "add_area"
    UPDATE_AREA = "update_area"
    ADD_ITEM = "add_item"
    MOVE_ITEM = "move_item"
    UPDATE_ITEM = "update_item"
    STORE_ITEM = "store_item"
    REMOVE_ITEM = "remove_item"
    RECORD_GROWTH_OBSERVATION = "record_growth_observation"
    PROPOSE_STAGE = "propose_stage"
    CONFIRM_STAGE = "confirm_stage"
    WITHDRAW_STAGE_PROPOSAL = "withdraw_stage_proposal"
    RECORD_CARE_OBSERVATION = "record_care_observation"
    COMFORT_CARE = "comfort_care"
    SET_REST_STATE = "set_rest_state"
    CORRECT_CARE_EVENT = "correct_care_event"
    UPDATE_FAMILY_THREAD = "update_family_thread"
    RECORD_RELATIONSHIP_EVENT = "record_relationship_event"
    SAVE_CONFIRMED_CARE_PLAN = "save_confirmed_care_plan"
    EXECUTE_CONFIRMED_CARE = "execute_confirmed_care"
    REQUEST_DELETION = "request_deletion"
    PROPOSE_NAME_CHANGE = "propose_name_change"
    CONFIRM_NAME_CHANGE = "confirm_name_change"
    WITHDRAW_NAME_CHANGE = "withdraw_name_change"
    UPDATE_OWN_CALLING_PREFERENCE = "update_own_calling_preference"
    PAUSE = "pause"
    RESUME = "resume"
    CONFIRM_DELETION = "confirm_deletion"
    CANCEL_DELETION = "cancel_deletion"
    FINALIZE_CLEANUP = "finalize_cleanup"


class OperationStatus(TextEnum):
    PENDING = "pending"
    PROCESSING = "processing"
    COMPLETED = "completed"
    FAILED_RETRYABLE = "failed_retryable"
    REJECTED = "rejected"
    CANCELLED = "cancelled"


class SnapshotKind(TextEnum):
    BEFORE = "before"
    AFTER = "after"


class SourceType(TextEnum):
    USER_INTERACTION = "user_interaction"
    EXTERNAL_AI_INTERACTION = "external_ai_interaction"
    # Canonical mailbox-write completion. It is immediately actionable and is
    # deliberately independent from Xinchao's later 30-minute silence clock.
    MAILBOX_EVENT = "mailbox_event"
    ANIMA_MEMORY = "anima_memory"
    INTERNAL_COMMAND = "internal_command"
    TEST_FIXTURE = "test_fixture"


class SourceStatus(TextEnum):
    ACCEPTED = "accepted"
    APPLIED = "applied"
    REJECTED = "rejected"
    REVOKED = "revoked"
    SUPERSEDED = "superseded"


class HealthOutcome(TextEnum):
    NO_CHANGE = "no_change"
    OBSERVE = "observe"
    MILD_ILLNESS_CANDIDATE = "mild_illness_candidate"
    MILD_DISCOMFORT_CANDIDATE = "mild_discomfort_candidate"
    MINOR_INJURY_CANDIDATE = "minor_injury_candidate"
    CARE = "care"
    IMPROVING = "improving"
    RECOVERED = "recovered"


class HealthTrigger(TextEnum):
    VALID_WAKE = "valid_wake"
    INTERACTION = "interaction"
    RELEVANT_MAILBOX_EVENT = "relevant_mailbox_event"
    PAGE_OPEN = "page_open"
    STATUS_READ = "status_read"
    REFRESH = "refresh"
    POLL = "poll"


class SafetyCategory(TextEnum):
    AUTHORIZATION = "authorization"
    IDEMPOTENCY = "idempotency"
    STATE = "state"
    HEALTH = "health"
    PRIVACY = "privacy"
    SOURCE = "source"


class NurseryError(Exception):
    """Stable domain error safe for adapters to translate later."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = str(code)
        self.message = str(message)
        self.details = dict(details or {})

    def as_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {"code": self.code, "message": self.message}
        if self.details:
            result["details"] = dict(self.details)
        return result


@dataclass(frozen=True)
class ActorContext:
    """Identity constructed by a trusted server adapter, never request JSON."""

    account_id: str
    caregiver_id: str
    role: CaregiverRole
    permissions: frozenset[str] = field(default_factory=frozenset)
    auth_source: AuthSource = AuthSource.TEST_FIXTURE

    @classmethod
    def build(
        cls,
        *,
        account_id: str,
        caregiver_id: str,
        role: CaregiverRole | str,
        permissions: Iterable[str] = (),
        auth_source: AuthSource | str = AuthSource.TEST_FIXTURE,
    ) -> "ActorContext":
        account = str(account_id).strip()
        caregiver = str(caregiver_id).strip()
        if not account or not caregiver:
            raise NurseryError(
                "INVALID_ACTOR", "account_id and caregiver_id are required"
            )
        return cls(
            account_id=account,
            caregiver_id=caregiver,
            role=CaregiverRole(role),
            permissions=frozenset(
                str(item).strip() for item in permissions if str(item).strip()
            ),
            auth_source=AuthSource(auth_source),
        )

    def has_permission(self, permission: str) -> bool:
        return permission in self.permissions or "nursery.*" in self.permissions


@dataclass(frozen=True)
class SourceReference:
    source_type: SourceType
    source_id: str
    source_version: str
    specification_ref: str = ""

    @classmethod
    def build(
        cls,
        *,
        source_type: SourceType | str,
        source_id: str,
        source_version: str,
        specification_ref: str = "",
    ) -> "SourceReference":
        source = str(source_id).strip()
        version = str(source_version).strip()
        if not source or not version:
            raise NurseryError(
                "INVALID_SOURCE", "source_id and source_version are required"
            )
        return cls(
            source_type=SourceType(source_type),
            source_id=source,
            source_version=version,
            specification_ref=str(specification_ref).strip(),
        )


@dataclass(frozen=True)
class NurseryCommand:
    actor: ActorContext
    child_id: str
    action: NurseryAction
    operation_id: str
    idempotency_key: str
    expected_state_version: int | None
    payload: dict[str, Any] = field(default_factory=dict)
    source: SourceReference | None = None
    private_payload: dict[str, Any] | None = field(
        default=None, repr=False, compare=False
    )


@dataclass(frozen=True)
class OperationResult:
    operation_id: str
    status: OperationStatus
    replayed: bool = False
    changed: bool = False
    module_state: ModuleState | None = None
    state_version: int | None = None
    result: dict[str, Any] = field(default_factory=dict)
    error_code: str = ""
    error_message: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "operation_id": self.operation_id,
            "status": self.status.value,
            "replayed": self.replayed,
            "changed": self.changed,
            "module_state": self.module_state.value if self.module_state else None,
            "state_version": self.state_version,
            "result": dict(self.result),
            "error": (
                {"code": self.error_code, "message": self.error_message}
                if self.error_code
                else None
            ),
        }


TERMINAL_OPERATION_STATUSES = frozenset(
    {
        OperationStatus.COMPLETED,
        OperationStatus.REJECTED,
        OperationStatus.CANCELLED,
    }
)


STATE_ACTIONS: dict[ModuleState, frozenset[NurseryAction]] = {
    ModuleState.NEVER_ENABLED: frozenset({NurseryAction.START_DRAFT}),
    ModuleState.DRAFT: frozenset(
        {
            NurseryAction.ACTIVATE_FIXTURE,
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
    ),
    ModuleState.ACTIVE: frozenset(
        {
            NurseryAction.SYNC_ANIMA_CONTEXT,
            NurseryAction.APPLY_ANIMA_FAMILY_EVENT,
            NurseryAction.CHILD_INTERACT,
            NurseryAction.ADD_AREA,
            NurseryAction.UPDATE_AREA,
            NurseryAction.ADD_ITEM,
            NurseryAction.MOVE_ITEM,
            NurseryAction.UPDATE_ITEM,
            NurseryAction.STORE_ITEM,
            NurseryAction.REMOVE_ITEM,
            NurseryAction.RECORD_GROWTH_OBSERVATION,
            NurseryAction.PROPOSE_STAGE,
            NurseryAction.CONFIRM_STAGE,
            NurseryAction.WITHDRAW_STAGE_PROPOSAL,
            NurseryAction.RECORD_CARE_OBSERVATION,
            NurseryAction.COMFORT_CARE,
        NurseryAction.SET_REST_STATE,
        NurseryAction.CORRECT_CARE_EVENT,
        NurseryAction.UPDATE_FAMILY_THREAD,
        NurseryAction.RECORD_RELATIONSHIP_EVENT,
            NurseryAction.SAVE_CONFIRMED_CARE_PLAN,
            NurseryAction.EXECUTE_CONFIRMED_CARE,
            NurseryAction.REQUEST_DELETION,
            NurseryAction.PROPOSE_NAME_CHANGE,
            NurseryAction.CONFIRM_NAME_CHANGE,
            NurseryAction.WITHDRAW_NAME_CHANGE,
            NurseryAction.UPDATE_OWN_CALLING_PREFERENCE,
            NurseryAction.SAVE_MODEL_CONNECTION,
            NurseryAction.RETEST_MODEL_CONNECTION,
            NurseryAction.DELETE_MODEL_CONNECTION,
            NurseryAction.PAUSE,
            NurseryAction.CONFIRM_DELETION,
        }
    ),
    ModuleState.PAUSED: frozenset(
        {
            NurseryAction.PAUSE,
            NurseryAction.RESUME,
            NurseryAction.SAVE_MODEL_CONNECTION,
            NurseryAction.RETEST_MODEL_CONNECTION,
            NurseryAction.DELETE_MODEL_CONNECTION,
            NurseryAction.REQUEST_DELETION,
            NurseryAction.CONFIRM_DELETION,
        }
    ),
    ModuleState.DELETION_PENDING: frozenset(
        {NurseryAction.CANCEL_DELETION, NurseryAction.FINALIZE_CLEANUP}
    ),
}


ACTION_PERMISSIONS: dict[NurseryAction, str] = {
    NurseryAction.START_DRAFT: "nursery.create_draft",
    NurseryAction.ACTIVATE_FIXTURE: "nursery.activate",
    NurseryAction.BIND_EXTERNAL_GUARDIAN: "nursery.guardian.bind",
    NurseryAction.SAVE_MODEL_CONNECTION: "nursery.model.configure",
    NurseryAction.RETEST_MODEL_CONNECTION: "nursery.model.configure",
    NurseryAction.DELETE_MODEL_CONNECTION: "nursery.model.configure",
    NurseryAction.SAVE_DRAFT_IDENTITY: "nursery.draft.edit",
    NurseryAction.SAVE_NAME_PROPOSALS: "nursery.draft.edit",
    NurseryAction.REVIEW_NAME_CANDIDATES: "nursery.draft.edit",
    NurseryAction.SELECT_DRAFT_NAME: "nursery.draft.edit",
    NurseryAction.SUBMIT_TEMPERAMENT_QUESTIONNAIRE: "nursery.draft.edit",
    NurseryAction.SUBMIT_INITIAL_STYLE: "nursery.draft.edit",
    NurseryAction.SAVE_INITIAL_SPACE: "nursery.draft.edit",
    NurseryAction.CONFIRM_CREATION: "nursery.creation.confirm",
    NurseryAction.SYNC_ANIMA_CONTEXT: "nursery.anima.context.sync",
    NurseryAction.APPLY_ANIMA_FAMILY_EVENT: "nursery.anima.family_event.apply",
    NurseryAction.CHILD_INTERACT: "nursery.child.interact",
    NurseryAction.ADD_AREA: "nursery.world.edit",
    NurseryAction.UPDATE_AREA: "nursery.world.edit",
    NurseryAction.ADD_ITEM: "nursery.world.edit",
    NurseryAction.MOVE_ITEM: "nursery.world.edit",
    NurseryAction.UPDATE_ITEM: "nursery.world.edit",
    NurseryAction.STORE_ITEM: "nursery.world.edit",
    NurseryAction.REMOVE_ITEM: "nursery.world.edit",
    NurseryAction.RECORD_GROWTH_OBSERVATION: "nursery.growth.observe",
    NurseryAction.PROPOSE_STAGE: "nursery.growth.propose",
    NurseryAction.CONFIRM_STAGE: "nursery.growth.confirm",
    NurseryAction.WITHDRAW_STAGE_PROPOSAL: "nursery.growth.propose",
    NurseryAction.RECORD_CARE_OBSERVATION: "nursery.care.observe",
    NurseryAction.COMFORT_CARE: "nursery.care.comfort",
    NurseryAction.SET_REST_STATE: "nursery.care.execute",
    NurseryAction.CORRECT_CARE_EVENT: "nursery.care.execute",
    NurseryAction.UPDATE_FAMILY_THREAD: "nursery.world.edit",
    NurseryAction.RECORD_RELATIONSHIP_EVENT: "nursery.care.comfort",
    NurseryAction.SAVE_CONFIRMED_CARE_PLAN: "nursery.care.plan",
    NurseryAction.EXECUTE_CONFIRMED_CARE: "nursery.care.execute",
    NurseryAction.REQUEST_DELETION: "nursery.delete.request",
    NurseryAction.PROPOSE_NAME_CHANGE: "nursery.profile.propose",
    NurseryAction.CONFIRM_NAME_CHANGE: "nursery.profile.confirm",
    NurseryAction.WITHDRAW_NAME_CHANGE: "nursery.profile.propose",
    NurseryAction.UPDATE_OWN_CALLING_PREFERENCE: "nursery.profile.own",
    NurseryAction.PAUSE: "nursery.pause",
    NurseryAction.RESUME: "nursery.resume",
    NurseryAction.CONFIRM_DELETION: "nursery.delete.confirm",
    NurseryAction.CANCEL_DELETION: "nursery.delete.cancel",
    NurseryAction.FINALIZE_CLEANUP: "nursery.cleanup",
}

ANIMA_MCP_TOOL_PERMISSION = "nursery.external.tools"

ANIMA_MCP_ALLOWED_ACTIONS = frozenset(
    {
        NurseryAction.SAVE_NAME_PROPOSALS,
        NurseryAction.REVIEW_NAME_CANDIDATES,
        NurseryAction.SELECT_DRAFT_NAME,
        NurseryAction.SUBMIT_TEMPERAMENT_QUESTIONNAIRE,
        NurseryAction.SUBMIT_INITIAL_STYLE,
        NurseryAction.SAVE_INITIAL_SPACE,
        NurseryAction.CONFIRM_CREATION,
        NurseryAction.CHILD_INTERACT,
        NurseryAction.ADD_AREA,
        NurseryAction.UPDATE_AREA,
        NurseryAction.ADD_ITEM,
        NurseryAction.MOVE_ITEM,
        NurseryAction.UPDATE_ITEM,
        NurseryAction.STORE_ITEM,
        NurseryAction.REMOVE_ITEM,
        NurseryAction.RECORD_GROWTH_OBSERVATION,
        NurseryAction.PROPOSE_STAGE,
        NurseryAction.CONFIRM_STAGE,
        NurseryAction.WITHDRAW_STAGE_PROPOSAL,
        NurseryAction.RECORD_CARE_OBSERVATION,
        NurseryAction.COMFORT_CARE,
        NurseryAction.SET_REST_STATE,
        NurseryAction.CORRECT_CARE_EVENT,
        NurseryAction.UPDATE_FAMILY_THREAD,
        NurseryAction.RECORD_RELATIONSHIP_EVENT,
        NurseryAction.EXECUTE_CONFIRMED_CARE,
        NurseryAction.REQUEST_DELETION,
        NurseryAction.CONFIRM_DELETION,
        NurseryAction.CANCEL_DELETION,
        NurseryAction.PAUSE,
        NurseryAction.RESUME,
        NurseryAction.PROPOSE_NAME_CHANGE,
        NurseryAction.CONFIRM_NAME_CHANGE,
        NurseryAction.WITHDRAW_NAME_CHANGE,
        NurseryAction.UPDATE_OWN_CALLING_PREFERENCE,
    }
)


def ensure_action_allowed(state: ModuleState | str, action: NurseryAction | str) -> None:
    current = ModuleState(state)
    requested = NurseryAction(action)
    if requested not in STATE_ACTIONS[current]:
        raise NurseryError(
            "INVALID_STATE_TRANSITION",
            f"{requested.value} is not allowed while nursery is {current.value}",
            details={"state": current.value, "action": requested.value},
        )


def ensure_actor_allowed(actor: ActorContext, action: NurseryAction | str) -> None:
    requested = NurseryAction(action)
    if requested in {
        NurseryAction.SYNC_ANIMA_CONTEXT,
        NurseryAction.APPLY_ANIMA_FAMILY_EVENT,
    }:
        if (
            actor.role != CaregiverRole.SYSTEM_EVENT
            or actor.auth_source != AuthSource.INTERNAL_EVENT
            or not actor.has_permission(ACTION_PERMISSIONS[requested])
        ):
            raise NurseryError(
                "ANIMA_CONTEXT_AUTH_REQUIRED",
                "Anima family events require the internal memory-management actor",
            )
        return
    if actor.role not in {
        CaregiverRole.USER_GUARDIAN,
        CaregiverRole.EXTERNAL_AI_GUARDIAN,
    }:
        raise NurseryError(
            "ACTOR_NOT_GUARDIAN", "this action requires a registered guardian"
        )
    if requested in {
        NurseryAction.BIND_EXTERNAL_GUARDIAN,
        NurseryAction.SAVE_MODEL_CONNECTION,
        NurseryAction.RETEST_MODEL_CONNECTION,
        NurseryAction.DELETE_MODEL_CONNECTION,
        NurseryAction.SAVE_CONFIRMED_CARE_PLAN,
    } and actor.role != CaregiverRole.USER_GUARDIAN:
        raise NurseryError(
            "USER_GUARDIAN_REQUIRED",
            "only the user guardian can bind a guardian, configure the child model, or save a confirmed care plan",
        )
    if (
        actor.role == CaregiverRole.EXTERNAL_AI_GUARDIAN
        and actor.auth_source == AuthSource.MCP_SESSION
    ):
        if (
            actor.has_permission(ANIMA_MCP_TOOL_PERMISSION)
            and requested in ANIMA_MCP_ALLOWED_ACTIONS
        ):
            return
        raise NurseryError(
            "PERMISSION_DENIED",
            "this action is not available through Anima's personal MCP nursery tools",
        )
    permission = ACTION_PERMISSIONS[requested]
    if not actor.has_permission(permission):
        raise NurseryError(
            "PERMISSION_DENIED", f"missing required permission: {permission}"
        )
    if (
        requested == NurseryAction.ACTIVATE_FIXTURE
        and actor.auth_source != AuthSource.TEST_FIXTURE
    ):
        raise NurseryError(
            "STAGE_NOT_AVAILABLE",
            "activation is reserved for stage-1 fixtures until stage 2",
        )


def state_after_action(
    current: ModuleState,
    action: NurseryAction,
    *,
    active_pause_count: int = 0,
    deletion_confirmed: bool = False,
    pre_delete_state: ModuleState | None = None,
    cleanup_due: bool = False,
    creation_confirmed: bool = False,
) -> ModuleState:
    """Pure transition function used by both coordinator tests and Store."""

    ensure_action_allowed(current, action)
    if action == NurseryAction.START_DRAFT:
        return ModuleState.DRAFT
    if action == NurseryAction.ACTIVATE_FIXTURE:
        return ModuleState.ACTIVE
    if action == NurseryAction.CONFIRM_CREATION:
        return ModuleState.ACTIVE if creation_confirmed else current
    if action in {
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
        NurseryAction.CHILD_INTERACT,
        NurseryAction.SYNC_ANIMA_CONTEXT,
        NurseryAction.APPLY_ANIMA_FAMILY_EVENT,
        NurseryAction.ADD_AREA,
        NurseryAction.UPDATE_AREA,
        NurseryAction.ADD_ITEM,
        NurseryAction.MOVE_ITEM,
        NurseryAction.UPDATE_ITEM,
        NurseryAction.STORE_ITEM,
        NurseryAction.REMOVE_ITEM,
        NurseryAction.RECORD_GROWTH_OBSERVATION,
        NurseryAction.PROPOSE_STAGE,
        NurseryAction.CONFIRM_STAGE,
        NurseryAction.WITHDRAW_STAGE_PROPOSAL,
        NurseryAction.RECORD_CARE_OBSERVATION,
        NurseryAction.COMFORT_CARE,
        NurseryAction.SET_REST_STATE,
        NurseryAction.CORRECT_CARE_EVENT,
        NurseryAction.UPDATE_FAMILY_THREAD,
        NurseryAction.RECORD_RELATIONSHIP_EVENT,
        NurseryAction.SAVE_CONFIRMED_CARE_PLAN,
        NurseryAction.EXECUTE_CONFIRMED_CARE,
        NurseryAction.PROPOSE_NAME_CHANGE,
        NurseryAction.CONFIRM_NAME_CHANGE,
        NurseryAction.WITHDRAW_NAME_CHANGE,
        NurseryAction.UPDATE_OWN_CALLING_PREFERENCE,
    }:
        return current
    if action == NurseryAction.PAUSE:
        return ModuleState.PAUSED
    if action == NurseryAction.RESUME:
        if active_pause_count:
            return ModuleState.PAUSED
        return ModuleState.ACTIVE
    if action == NurseryAction.CONFIRM_DELETION:
        return ModuleState.DELETION_PENDING if deletion_confirmed else current
    if action == NurseryAction.CANCEL_DELETION:
        if active_pause_count:
            return ModuleState.PAUSED
        return (
            ModuleState.ACTIVE
            if pre_delete_state in {ModuleState.ACTIVE, ModuleState.PAUSED}
            else ModuleState.ACTIVE
        )
    if action == NurseryAction.FINALIZE_CLEANUP:
        if not cleanup_due:
            raise NurseryError(
                "CLEANUP_NOT_DUE", "deletion retention deadline has not elapsed"
            )
        return ModuleState.NEVER_ENABLED
    raise NurseryError("UNKNOWN_ACTION", f"unsupported action: {action.value}")
