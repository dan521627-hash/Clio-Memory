"""Small in-memory BM25 index for Chinese and mixed-language memory search."""

import hashlib
import logging
import re

import jieba


logger = logging.getLogger("ombre_brain.bm25")


class BM25Index:
    """Build a lazy BM25 index over the current visible candidate buckets."""

    def __init__(self, config: dict):
        matching = config.get("matching", {})
        self.enabled = bool(matching.get("bm25_enabled", True))
        self.max_content_chars = max(
            1000, min(50000, int(matching.get("bm25_max_content_chars", 12000)))
        )
        self._signature = ""
        self._ids: list[str] = []
        self._index = None
        self._available = True

    @staticmethod
    def _tokens(text: str) -> list[str]:
        normalized = str(text or "").casefold()
        words = []
        for token in jieba.lcut_for_search(normalized):
            clean = token.strip()
            if not clean or clean.isspace():
                continue
            if re.fullmatch(r"[\W_]+", clean, flags=re.UNICODE):
                continue
            words.append(clean)
        return words

    def _bucket_text(self, bucket: dict) -> str:
        metadata = bucket.get("metadata", {})
        name = str(metadata.get("name", ""))
        domains = " ".join(str(item) for item in metadata.get("domain", []))
        tags = " ".join(str(item) for item in metadata.get("tags", []))
        content = str(bucket.get("content", ""))[: self.max_content_chars]
        return f"{name} {name} {name} {domains} {domains} {tags} {tags} {content}"

    def _corpus_signature(self, buckets: list[dict]) -> str:
        digest = hashlib.sha256()
        for bucket in buckets:
            digest.update(str(bucket.get("id", "")).encode("utf-8"))
            digest.update(b"\0")
            digest.update(self._bucket_text(bucket).encode("utf-8"))
            digest.update(b"\0")
        return digest.hexdigest()

    def _ensure_index(self, buckets: list[dict]) -> None:
        signature = self._corpus_signature(buckets)
        if signature == self._signature:
            return
        try:
            from rank_bm25 import BM25Okapi
        except ImportError:
            self._available = False
            self._index = None
            logger.warning("rank_bm25 is unavailable; fuzzy and semantic search remain active")
            return
        self._ids = [str(bucket.get("id", "")) for bucket in buckets]
        corpus = [self._tokens(self._bucket_text(bucket)) for bucket in buckets]
        self._index = BM25Okapi(corpus) if corpus else None
        self._signature = signature
        self._available = True

    def scores(self, query: str, buckets: list[dict]) -> dict[str, float]:
        if not self.enabled or not buckets or not str(query).strip():
            return {}
        self._ensure_index(buckets)
        if not self._available or self._index is None:
            return {}
        tokens = self._tokens(query)
        if not tokens:
            return {}
        raw_scores = [max(0.0, float(value)) for value in self._index.get_scores(tokens)]
        highest = max(raw_scores, default=0.0)
        if highest <= 0:
            return {}
        return {
            bucket_id: score / highest
            for bucket_id, score in zip(self._ids, raw_scores)
            if score > 0
        }
