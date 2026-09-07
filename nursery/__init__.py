"""Local stage-1 and stage-2 backend core for Anima's optional nursery module.

The private browser routes are wired through Anima's existing entrypoints.
Nursery MCP tools register on Anima's existing personal MCP service; they do
not create a second endpoint, account, or authorization flow.
"""

from .anima_bridge import AnimaNurseryBridge
from .coordinator import NurseryCoordinator
from .creation_queries import NurseryCreationQueryService
from .creation_service import NurseryCreationService
from .interaction_service import NurseryInteractionService
from .models import (
    ActorContext,
    AuthSource,
    CaregiverRole,
    ModuleState,
    NurseryError,
    OperationStatus,
)
from .queries import NurseryQueryService
from .store import NurseryStore

__all__ = [
    "ActorContext",
    "AnimaNurseryBridge",
    "AuthSource",
    "CaregiverRole",
    "ModuleState",
    "NurseryCoordinator",
    "NurseryCreationQueryService",
    "NurseryCreationService",
    "NurseryError",
    "NurseryInteractionService",
    "NurseryQueryService",
    "NurseryStore",
    "OperationStatus",
]
