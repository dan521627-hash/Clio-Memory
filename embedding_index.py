"""Local embedding generation and SQLite-backed vector index."""

import asyncio
import hashlib
import logging
import os
import sqlite3
import threading
import time
from collections import OrderedDict
from contextlib import contextmanager
from pathlib import Path

import numpy as np

from memory_segments import split_memory_segments
from utils import now_iso


logger = logging.getLogger("ombre_brain.embeddings")


class EmbeddingIndex:
    """Keep bucket embeddings in a small SQLite sidecar database."""

    def __init__(self, config: dict):
        cfg = config.get("embeddings", {})
        self.enabled = bool(cfg.get("enabled", True))
        self.model_name = cfg.get("model", "BAAI/bge-small-zh-v1.5")
        self.dimensions = int(cfg.get("dimensions", 512))
        self.batch_size = int(cfg.get("batch_size", 8))
        self.cache_dir = cfg.get("cache_dir") or os.environ.get(
            "OMBRE_EMBEDDING_CACHE_DIR", "/models"
        )
        self.db_path = cfg.get("db_path") or os.environ.get(
            "OMBRE_EMBEDDING_DB",
            os.path.join(config["buckets_dir"], "embeddings.sqlite3"),
        )
        self._model = None
        self._model_lock = threading.Lock()
        self._segment_cache_lock = threading.Lock()
        self._segment_vector_cache: OrderedDict[str, np.ndarray] = OrderedDict()
        self._segment_cache_limit = 2048
        self._bucket_loader = None

        if self.enabled:
            self._init_db()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.db_path, timeout=30)
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA synchronous=NORMAL")
        return connection

    @contextmanager
    def _connection(self):
        connection = self._connect()
        try:
            yield connection
            connection.commit()
        finally:
            connection.close()

    def _init_db(self) -> None:
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        with self._connection() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS embeddings (
                    bucket_id TEXT PRIMARY KEY,
                    content_hash TEXT NOT NULL,
                    model TEXT NOT NULL,
                    dimensions INTEGER NOT NULL,
                    vector BLOB NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_embeddings_model "
                "ON embeddings(model, dimensions)"
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS embedding_queue (
                    bucket_id TEXT PRIMARY KEY,
                    content_hash TEXT NOT NULL,
                    queued_at TEXT NOT NULL,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    next_retry_at REAL NOT NULL DEFAULT 0,
                    last_error TEXT NOT NULL DEFAULT ''
                )
                """
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_embedding_queue_retry "
                "ON embedding_queue(next_retry_at, queued_at)"
            )

    def set_bucket_loader(self, loader) -> None:
        """Attach a synchronous bucket loader used by the durable worker."""
        self._bucket_loader = loader

    def _get_model(self):
        if self._model is not None:
            return self._model
        with self._model_lock:
            if self._model is None:
                from fastembed import TextEmbedding

                Path(self.cache_dir).mkdir(parents=True, exist_ok=True)
                logger.info("Loading embedding model: %s", self.model_name)
                self._model = TextEmbedding(
                    model_name=self.model_name,
                    cache_dir=self.cache_dir,
                    threads=max(1, min(4, os.cpu_count() or 1)),
                )
        return self._model

    @staticmethod
    def bucket_text(bucket: dict) -> str:
        meta = bucket.get("metadata", {})
        parts = [
            f"name: {meta.get('name', '')}",
            f"domain: {', '.join(meta.get('domain', []))}",
            f"tags: {', '.join(meta.get('tags', []))}",
            f"content: {bucket.get('content', '')}",
        ]
        return "\n".join(parts).strip()

    @staticmethod
    def content_hash(text: str) -> str:
        return hashlib.sha256(text.encode("utf-8")).hexdigest()

    @staticmethod
    def _normalize(vector) -> np.ndarray:
        result = np.asarray(vector, dtype=np.float32)
        norm = float(np.linalg.norm(result))
        return result / norm if norm else result

    def _passage_vectors(self, texts: list[str]) -> list[np.ndarray]:
        model = self._get_model()
        vectors = model.passage_embed(texts, batch_size=self.batch_size)
        return [self._normalize(vector) for vector in vectors]

    def _query_vector(self, query: str) -> np.ndarray:
        model = self._get_model()
        vector = next(iter(model.query_embed([query])))
        return self._normalize(vector)

    async def embed_passage(self, text: str) -> np.ndarray:
        """Embed one sidecar record without persisting it in the bucket index."""
        if not self.enabled:
            raise RuntimeError("向量模型未启用")
        vectors = await asyncio.to_thread(
            self._passage_vectors, [str(text or "")]
        )
        return vectors[0]

    async def embed_query(self, text: str) -> np.ndarray:
        """Embed one query for sidecar semantic search."""
        if not self.enabled:
            raise RuntimeError("向量模型未启用")
        return await asyncio.to_thread(self._query_vector, str(text or ""))

    def _upsert_rows(self, rows: list[tuple[str, str, np.ndarray]]) -> None:
        now = now_iso()
        values = []
        for bucket_id, digest, vector in rows:
            if len(vector) != self.dimensions:
                raise ValueError(
                    f"Unexpected embedding size {len(vector)}; expected {self.dimensions}"
                )
            values.append(
                (
                    bucket_id,
                    digest,
                    self.model_name,
                    self.dimensions,
                    vector.astype("<f4", copy=False).tobytes(),
                    now,
                )
            )
        with self._connection() as connection:
            connection.executemany(
                """
                INSERT INTO embeddings
                    (bucket_id, content_hash, model, dimensions, vector, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(bucket_id) DO UPDATE SET
                    content_hash=excluded.content_hash,
                    model=excluded.model,
                    dimensions=excluded.dimensions,
                    vector=excluded.vector,
                    updated_at=excluded.updated_at
                """,
                values,
            )

    def _stored_hashes(self) -> dict[str, str]:
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT bucket_id, content_hash FROM embeddings "
                "WHERE model=? AND dimensions=?",
                (self.model_name, self.dimensions),
            ).fetchall()
        return dict(rows)

    def _upsert_bucket_sync(self, bucket: dict) -> bool:
        text = self.bucket_text(bucket)
        digest = self.content_hash(text)
        if self._stored_hashes().get(bucket["id"]) == digest:
            return False
        vector = self._passage_vectors([text])[0]
        self._upsert_rows([(bucket["id"], digest, vector)])
        return True

    async def upsert_bucket(self, bucket: dict) -> bool:
        if not self.enabled or not bucket:
            return False
        return await asyncio.to_thread(self._upsert_bucket_sync, bucket)

    def _enqueue_bucket_sync(self, bucket: dict) -> bool:
        text = self.bucket_text(bucket)
        digest = self.content_hash(text)
        bucket_id = str(bucket["id"])
        if self._stored_hashes().get(bucket_id) == digest:
            with self._connection() as connection:
                connection.execute(
                    "DELETE FROM embedding_queue WHERE bucket_id=?", (bucket_id,)
                )
            return False
        with self._connection() as connection:
            connection.execute(
                """
                INSERT INTO embedding_queue
                    (bucket_id, content_hash, queued_at, attempts, next_retry_at, last_error)
                VALUES (?, ?, ?, 0, 0, '')
                ON CONFLICT(bucket_id) DO UPDATE SET
                    content_hash=excluded.content_hash,
                    queued_at=excluded.queued_at,
                    attempts=0,
                    next_retry_at=0,
                    last_error=''
                """,
                (bucket_id, digest, now_iso()),
            )
        return True

    async def enqueue_bucket(self, bucket: dict) -> bool:
        """Durably queue an embedding update without delaying the memory write."""
        if not self.enabled or not bucket:
            return False
        return await asyncio.to_thread(self._enqueue_bucket_sync, bucket)

    def _process_queue_sync(self, limit: int = 8) -> dict:
        if self._bucket_loader is None:
            return {"processed": 0, "failed": 0, "remaining": self.pending_count()}
        now = time.time()
        with self._connection() as connection:
            rows = connection.execute(
                """
                SELECT bucket_id, content_hash, attempts
                FROM embedding_queue
                WHERE next_retry_at<=?
                ORDER BY queued_at, bucket_id
                LIMIT ?
                """,
                (now, max(1, int(limit))),
            ).fetchall()

        processed = 0
        failed = 0
        for bucket_id, queued_hash, attempts in rows:
            try:
                bucket = self._bucket_loader(bucket_id)
                if not bucket:
                    with self._connection() as connection:
                        connection.execute(
                            "DELETE FROM embedding_queue WHERE bucket_id=?",
                            (bucket_id,),
                        )
                        connection.execute(
                            "DELETE FROM embeddings WHERE bucket_id=?", (bucket_id,)
                        )
                    processed += 1
                    continue
                current_hash = self.content_hash(self.bucket_text(bucket))
                if current_hash != queued_hash:
                    self._enqueue_bucket_sync(bucket)
                    continue
                self._upsert_bucket_sync(bucket)
                with self._connection() as connection:
                    connection.execute(
                        "DELETE FROM embedding_queue "
                        "WHERE bucket_id=? AND content_hash=?",
                        (bucket_id, queued_hash),
                    )
                processed += 1
            except Exception as error:
                failed += 1
                next_attempt = int(attempts) + 1
                delay = min(3600, 15 * (2 ** min(next_attempt - 1, 8)))
                with self._connection() as connection:
                    connection.execute(
                        """
                        UPDATE embedding_queue
                        SET attempts=?, next_retry_at=?, last_error=?
                        WHERE bucket_id=? AND content_hash=?
                        """,
                        (
                            next_attempt,
                            time.time() + delay,
                            str(error)[:500],
                            bucket_id,
                            queued_hash,
                        ),
                    )
                logger.warning(
                    "Embedding queue retry scheduled for %s in %ss: %s",
                    bucket_id,
                    delay,
                    error,
                )
        return {
            "processed": processed,
            "failed": failed,
            "remaining": self.pending_count(),
        }

    async def process_queue(self, limit: int = 8) -> dict:
        if not self.enabled:
            return {"processed": 0, "failed": 0, "remaining": 0}
        return await asyncio.to_thread(self._process_queue_sync, limit)

    async def worker_loop(self, interval_seconds: int = 5) -> None:
        """Continuously drain durable embedding work until the service stops."""
        while True:
            result = await self.process_queue(limit=self.batch_size)
            await asyncio.sleep(0.25 if result["processed"] else interval_seconds)

    def _backfill_sync(self, buckets: list[dict]) -> dict:
        stored = self._stored_hashes()
        pending = []
        for bucket in buckets:
            text = self.bucket_text(bucket)
            digest = self.content_hash(text)
            if stored.get(bucket["id"]) != digest:
                pending.append((bucket["id"], digest, text))

        for offset in range(0, len(pending), self.batch_size):
            batch = pending[offset : offset + self.batch_size]
            vectors = self._passage_vectors([item[2] for item in batch])
            self._upsert_rows(
                [
                    (bucket_id, digest, vector)
                    for (bucket_id, digest, _), vector in zip(batch, vectors)
                ]
            )

        live_ids = {bucket["id"] for bucket in buckets}
        with self._connection() as connection:
            indexed_ids = {
                row[0] for row in connection.execute("SELECT bucket_id FROM embeddings")
            }
            orphan_ids = indexed_ids - live_ids
            if orphan_ids:
                connection.executemany(
                    "DELETE FROM embeddings WHERE bucket_id=?",
                    [(bucket_id,) for bucket_id in orphan_ids],
                )
            if live_ids:
                connection.executemany(
                    "DELETE FROM embedding_queue WHERE bucket_id=?",
                    [(bucket_id,) for bucket_id in live_ids],
                )

        return {
            "total": len(buckets),
            "updated": len(pending),
            "unchanged": len(buckets) - len(pending),
            "removed": len(orphan_ids),
        }

    async def backfill(self, buckets: list[dict]) -> dict:
        if not self.enabled:
            return {"total": len(buckets), "updated": 0, "unchanged": 0, "removed": 0}
        return await asyncio.to_thread(self._backfill_sync, buckets)

    def _query_scores_with_vector_sync(
        self, query: str
    ) -> tuple[dict[str, float], np.ndarray]:
        query_vector = self._query_vector(query)
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT bucket_id, vector FROM embeddings "
                "WHERE model=? AND dimensions=?",
                (self.model_name, self.dimensions),
            ).fetchall()

        scores = {}
        for bucket_id, blob in rows:
            vector = np.frombuffer(blob, dtype="<f4")
            if len(vector) == self.dimensions:
                scores[bucket_id] = float(np.dot(query_vector, vector))
        return scores, query_vector

    def _query_scores_sync(self, query: str) -> dict[str, float]:
        scores, _ = self._query_scores_with_vector_sync(query)
        return scores

    async def query_scores(self, query: str) -> dict[str, float]:
        if not self.enabled:
            return {}
        return await asyncio.to_thread(self._query_scores_sync, query)

    async def query_scores_with_vector(
        self, query: str
    ) -> tuple[dict[str, float], np.ndarray | None]:
        if not self.enabled:
            return {}, None
        return await asyncio.to_thread(self._query_scores_with_vector_sync, query)

    def _segment_vectors(self, texts: list[str]) -> list[np.ndarray]:
        keys = [self.content_hash(text) for text in texts]
        vectors: list[np.ndarray | None] = [None] * len(texts)
        missing_indexes = []
        with self._segment_cache_lock:
            for index, key in enumerate(keys):
                cached = self._segment_vector_cache.get(key)
                if cached is None:
                    missing_indexes.append(index)
                else:
                    self._segment_vector_cache.move_to_end(key)
                    vectors[index] = cached

        if missing_indexes:
            generated = self._passage_vectors([texts[index] for index in missing_indexes])
            with self._segment_cache_lock:
                for index, vector in zip(missing_indexes, generated):
                    key = keys[index]
                    self._segment_vector_cache[key] = vector
                    self._segment_vector_cache.move_to_end(key)
                    vectors[index] = vector
                while len(self._segment_vector_cache) > self._segment_cache_limit:
                    self._segment_vector_cache.popitem(last=False)
        return [vector for vector in vectors if vector is not None]

    def _query_segment_matches_sync(
        self, query: str, buckets: list[dict]
    ) -> tuple[dict[str, dict], np.ndarray]:
        query_vector = self._query_vector(query)
        records = []
        texts = []
        for bucket in buckets:
            metadata = bucket.get("metadata", {})
            prefix = (
                f"name: {metadata.get('name', '')}\n"
                f"domain: {', '.join(metadata.get('domain', []))}\n"
                f"tags: {', '.join(metadata.get('tags', []))}\n"
            )
            segments = split_memory_segments(
                bucket.get("content", ""), metadata.get("created", "")
            )
            for segment in segments:
                records.append((bucket["id"], segment, len(segments)))
                texts.append(prefix + segment["content"])

        if not texts:
            return {}, query_vector
        vectors = self._segment_vectors(texts)
        matches: dict[str, dict] = {}
        for (bucket_id, segment, total), vector in zip(records, vectors):
            score = float(np.dot(query_vector, vector))
            current = matches.get(bucket_id)
            if current is None or score > current["score"]:
                matches[bucket_id] = {
                    "score": score,
                    "segment": {**segment, "total_segments": total},
                }
        return matches, query_vector

    async def query_segment_matches(
        self, query: str, buckets: list[dict]
    ) -> tuple[dict[str, dict], np.ndarray | None]:
        """Return each bucket's most relevant packet without persisting new data."""
        if not self.enabled:
            return {}, None
        return await asyncio.to_thread(
            self._query_segment_matches_sync, query, buckets
        )

    def _pairwise_scores_sync(
        self, bucket_ids: list[str]
    ) -> dict[tuple[str, str], float]:
        requested = set(bucket_ids)
        if not requested:
            return {}
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT bucket_id, vector FROM embeddings "
                "WHERE model=? AND dimensions=?",
                (self.model_name, self.dimensions),
            ).fetchall()
        vectors = {
            bucket_id: np.frombuffer(blob, dtype="<f4")
            for bucket_id, blob in rows
            if bucket_id in requested
        }
        ids = sorted(vectors)
        scores = {}
        for index, left_id in enumerate(ids):
            left = vectors[left_id]
            if len(left) != self.dimensions:
                continue
            for right_id in ids[index + 1 :]:
                right = vectors[right_id]
                if len(right) == self.dimensions:
                    scores[(left_id, right_id)] = float(np.dot(left, right))
        return scores

    async def pairwise_scores(
        self, bucket_ids: list[str]
    ) -> dict[tuple[str, str], float]:
        """Return stored-vector cosine scores without generating new vectors."""
        if not self.enabled:
            return {}
        return await asyncio.to_thread(self._pairwise_scores_sync, bucket_ids)

    def _neighbors_for_bucket_sync(
        self,
        bucket_id: str,
        candidate_ids: list[str] | None = None,
    ) -> list[tuple[str, float]]:
        allowed = set(candidate_ids) if candidate_ids is not None else None
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT bucket_id, vector FROM embeddings "
                "WHERE model=? AND dimensions=?",
                (self.model_name, self.dimensions),
            ).fetchall()
        vectors = {
            row_id: np.frombuffer(blob, dtype="<f4")
            for row_id, blob in rows
            if allowed is None or row_id == bucket_id or row_id in allowed
        }
        source = vectors.get(bucket_id)
        if source is None or len(source) != self.dimensions:
            return []
        neighbors = []
        for other_id, vector in vectors.items():
            if other_id != bucket_id and len(vector) == self.dimensions:
                neighbors.append((other_id, float(np.dot(source, vector))))
        neighbors.sort(key=lambda item: (-item[1], item[0]))
        return neighbors

    async def neighbors_for_bucket(
        self,
        bucket_id: str,
        candidate_ids: list[str] | None = None,
    ) -> list[tuple[str, float]]:
        """Find neighbors using vectors already stored in SQLite."""
        if not self.enabled:
            return []
        return await asyncio.to_thread(
            self._neighbors_for_bucket_sync, bucket_id, candidate_ids
        )

    async def delete(self, bucket_id: str) -> None:
        if not self.enabled:
            return

        def delete_sync():
            with self._connection() as connection:
                connection.execute("DELETE FROM embeddings WHERE bucket_id=?", (bucket_id,))
                connection.execute(
                    "DELETE FROM embedding_queue WHERE bucket_id=?", (bucket_id,)
                )

        await asyncio.to_thread(delete_sync)

    def count(self) -> int:
        if not self.enabled:
            return 0
        with self._connection() as connection:
            return int(connection.execute("SELECT COUNT(*) FROM embeddings").fetchone()[0])

    def pending_count(self) -> int:
        if not self.enabled:
            return 0
        with self._connection() as connection:
            return int(
                connection.execute("SELECT COUNT(*) FROM embedding_queue").fetchone()[0]
            )
