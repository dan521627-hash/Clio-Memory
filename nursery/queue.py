"""Per-child process-local locks; SQLite remains the final authority."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass


@dataclass(frozen=True)
class QueueConfig:
    lock_wait_seconds: float = 0.25
    replay_wait_seconds: float = 0.5
    lease_seconds: float = 30.0
    poll_interval_seconds: float = 0.01

    def __post_init__(self) -> None:
        for name, value in (
            ("lock_wait_seconds", self.lock_wait_seconds),
            ("replay_wait_seconds", self.replay_wait_seconds),
            ("lease_seconds", self.lease_seconds),
            ("poll_interval_seconds", self.poll_interval_seconds),
        ):
            if value <= 0:
                raise ValueError(f"{name} must be positive")


class ChildLockRegistry:
    def __init__(self) -> None:
        self._locks: dict[str, asyncio.Lock] = {}
        self._guard = asyncio.Lock()

    async def get(self, child_id: str) -> asyncio.Lock:
        async with self._guard:
            lock = self._locks.get(child_id)
            if lock is None:
                lock = asyncio.Lock()
                self._locks[child_id] = lock
            return lock
