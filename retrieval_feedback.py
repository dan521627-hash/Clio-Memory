"""Query-specific retrieval feedback stored outside Markdown memory buckets."""

import asyncio
import hashlib
import math
import os
import secrets
import sqlite3
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from utils import now_iso


@dataclass(frozen=True)
class PendingSearch:
    created_at: float
    result_ids: frozenset[str]
    query_hash: str
    query_vector: bytes


class RetrievalFeedbackStore:
    """Keep bounded query-specific ranking feedback in a SQLite sidecar."""

    def __init__(self, config: dict, model_name: str, dimensions: int):
        settings = config.get("retrieval_feedback", {})
        self.enabled = bool(settings.get("enabled", False))
        self.model_name = str(model_name)
        self.dimensions = int(dimensions)
        self.query_similarity_threshold = max(
            0.0,
            min(1.0, float(settings.get("query_similarity_threshold", 0.82))),
        )
        self.max_adjustment = max(
            0.0, min(20.0, float(settings.get("max_adjustment", 5.0)))
        )
        self.pending_ttl_seconds = max(
            60, int(float(settings.get("pending_ttl_minutes", 30)) * 60)
        )
        self.max_pending = max(16, int(settings.get("max_pending", 256)))
        self.db_path = settings.get("db_path") or os.environ.get(
            "OMBRE_RETRIEVAL_FEEDBACK_DB",
            os.path.join(config["buckets_dir"], "retrieval_feedback.sqlite3"),
        )
        self._pending: OrderedDict[str, PendingSearch] = OrderedDict()
        self._pending_lock = threading.RLock()
        if self.enabled:
            self._init_db()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.db_path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA synchronous=NORMAL")
        return connection

    def _init_db(self) -> None:
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS retrieval_feedback (
                    retrieval_id TEXT NOT NULL,
                    bucket_id TEXT NOT NULL,
                    query_hash TEXT NOT NULL,
                    model TEXT NOT NULL,
                    dimensions INTEGER NOT NULL,
                    query_vector BLOB NOT NULL,
                    rating INTEGER NOT NULL CHECK (rating IN (-1, 1)),
                    source TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (retrieval_id, bucket_id)
                )
                """
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_retrieval_feedback_bucket "
                "ON retrieval_feedback(bucket_id, model, dimensions)"
            )

    def _normalized_vector(self, vector) -> np.ndarray:
        result = np.asarray(vector, dtype=np.float32)
        if len(result) != self.dimensions:
            raise ValueError(
                f"Unexpected feedback vector size {len(result)}; "
                f"expected {self.dimensions}"
            )
        norm = float(np.linalg.norm(result))
        return result / norm if norm else result

    def _cleanup_pending_locked(self, now: float) -> None:
        expired = [
            retrieval_id
            for retrieval_id, item in self._pending.items()
            if now - item.created_at > self.pending_ttl_seconds
        ]
        for retrieval_id in expired:
            self._pending.pop(retrieval_id, None)
        while len(self._pending) >= self.max_pending:
            self._pending.popitem(last=False)

    def begin_search(self, query_vector, result_ids: list[str]) -> str:
        """Create a short-lived ticket without persisting the raw query text."""
        if not self.enabled:
            return ""
        ordered_ids = tuple(
            dict.fromkeys(str(bucket_id).strip() for bucket_id in result_ids if bucket_id)
        )
        if not ordered_ids:
            return ""
        vector = self._normalized_vector(query_vector)
        vector_bytes = vector.astype("<f4", copy=False).tobytes()
        query_hash = hashlib.sha256(vector_bytes).hexdigest()
        now = time.monotonic()
        with self._pending_lock:
            self._cleanup_pending_locked(now)
            retrieval_id = secrets.token_hex(6)
            while retrieval_id in self._pending:
                retrieval_id = secrets.token_hex(6)
            self._pending[retrieval_id] = PendingSearch(
                created_at=now,
                result_ids=frozenset(ordered_ids),
                query_hash=query_hash,
                query_vector=vector_bytes,
            )
        return retrieval_id

    def _pending_search(self, retrieval_id: str) -> PendingSearch | None:
        now = time.monotonic()
        with self._pending_lock:
            self._cleanup_pending_locked(now)
            item = self._pending.get(retrieval_id)
            if item is not None:
                self._pending.move_to_end(retrieval_id)
            return item

    def _record_sync(
        self,
        retrieval_id: str,
        bucket_id: str,
        rating: int,
        source: str,
        pending: PendingSearch,
    ) -> dict:
        now = now_iso()
        with self._connect() as connection:
            existing = connection.execute(
                "SELECT rating FROM retrieval_feedback "
                "WHERE retrieval_id=? AND bucket_id=?",
                (retrieval_id, bucket_id),
            ).fetchone()
            connection.execute(
                """
                INSERT INTO retrieval_feedback (
                    retrieval_id, bucket_id, query_hash, model, dimensions,
                    query_vector, rating, source, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(retrieval_id, bucket_id) DO UPDATE SET
                    rating=excluded.rating,
                    source=excluded.source,
                    updated_at=excluded.updated_at
                """,
                (
                    retrieval_id,
                    bucket_id,
                    pending.query_hash,
                    self.model_name,
                    self.dimensions,
                    pending.query_vector,
                    rating,
                    source,
                    now,
                    now,
                ),
            )
        if existing is None:
            status = "recorded"
        elif int(existing["rating"]) == rating:
            status = "unchanged"
        else:
            status = "updated"
        return {"status": status, "rating": rating}

    async def record(
        self,
        retrieval_id: str,
        bucket_id: str,
        rating: int,
        source: str,
    ) -> dict:
        if not self.enabled:
            return {"status": "disabled"}
        retrieval_id = str(retrieval_id or "").strip()
        bucket_id = str(bucket_id or "").strip()
        if rating not in (-1, 1):
            raise ValueError("rating must be 1 or -1")
        pending = self._pending_search(retrieval_id)
        if pending is None:
            return {"status": "expired"}
        if bucket_id not in pending.result_ids:
            return {"status": "not_in_results"}
        safe_source = "recall" if source == "recall" else "explicit"
        return await asyncio.to_thread(
            self._record_sync,
            retrieval_id,
            bucket_id,
            rating,
            safe_source,
            pending,
        )

    def _adjustments_sync(
        self, query_vector: np.ndarray, candidate_ids: set[str]
    ) -> dict[str, float]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT bucket_id, query_vector, rating FROM retrieval_feedback "
                "WHERE model=? AND dimensions=?",
                (self.model_name, self.dimensions),
            ).fetchall()

        weighted_votes: dict[str, float] = {}
        denominator = max(1e-9, 1.0 - self.query_similarity_threshold)
        for row in rows:
            bucket_id = row["bucket_id"]
            if bucket_id not in candidate_ids:
                continue
            stored = np.frombuffer(row["query_vector"], dtype="<f4")
            if len(stored) != self.dimensions:
                continue
            similarity = float(np.dot(query_vector, stored))
            if similarity < self.query_similarity_threshold:
                continue
            relevance = min(
                1.0,
                max(0.0, (similarity - self.query_similarity_threshold) / denominator),
            )
            weighted_votes[bucket_id] = weighted_votes.get(bucket_id, 0.0) + (
                int(row["rating"]) * relevance
            )

        return {
            bucket_id: round(self.max_adjustment * math.tanh(vote), 4)
            for bucket_id, vote in weighted_votes.items()
            if abs(vote) > 1e-9
        }

    async def adjustments(
        self, query_vector, candidate_ids: list[str]
    ) -> dict[str, float]:
        if not self.enabled or not candidate_ids:
            return {}
        vector = self._normalized_vector(query_vector)
        return await asyncio.to_thread(
            self._adjustments_sync,
            vector,
            set(candidate_ids),
        )

    def count(self) -> int:
        if not self.enabled:
            return 0
        with self._connect() as connection:
            return int(
                connection.execute(
                    "SELECT COUNT(*) FROM retrieval_feedback"
                ).fetchone()[0]
            )
