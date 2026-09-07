"""Side-effect-free, privacy-trimmed stage-2 creation reads."""

from __future__ import annotations

from .models import ActorContext
from .store import NurseryStore


class NurseryCreationQueryService:
    def __init__(self, store: NurseryStore) -> None:
        self.store = store

    def get_draft(self, *, actor: ActorContext, child_id: str) -> dict:
        return self.store.get_creation_draft_view(actor=actor, child_id=child_id)
