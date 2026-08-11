"""Hybrid keyword and semantic retrieval for mailbox messages."""

from __future__ import annotations

import logging
import re


logger = logging.getLogger("ombre_brain.mailbox_search")


def _keyword_score(query: str, text: str) -> float:
    needle = re.sub(r"\s+", "", query).casefold()
    haystack = re.sub(r"\s+", "", text).casefold()
    if not needle or not haystack:
        return 0.0
    if needle in haystack:
        return 1.0
    units = set(re.findall(r"[a-z0-9]+|[\u4e00-\u9fff]{2}", needle))
    if not units and len(needle) >= 2:
        units = {needle[index : index + 2] for index in range(len(needle) - 1)}
    if not units:
        return 0.0
    return sum(1 for unit in units if unit in haystack) / len(units)


async def search_mailbox(
    store,
    embedding_index,
    query: str,
    *,
    limit: int = 5,
    include_deleted: bool = False,
    exclude_ids: set[int] | None = None,
) -> list[dict]:
    """Return ranked messages without modifying mailbox data or its index."""
    clean_query = str(query or "").strip()
    if not clean_query:
        return []
    safe_limit = max(1, min(20, int(limit)))
    excluded = {int(value) for value in (exclude_ids or set())}
    messages = await store.search_pool(include_deleted=include_deleted, limit=1000)
    messages = [item for item in messages if int(item["message_id"]) not in excluded]
    if not messages:
        return []

    semantic_scores: dict[str, dict] = {}
    semantic_available = False
    if embedding_index is not None and getattr(embedding_index, "enabled", False):
        buckets = [
            {
                "id": f"mailbox:{item['message_id']}",
                "metadata": {
                    "name": f"信箱留言 {item['message_id']}",
                    "created": item["created_at"],
                    "domain": ["信箱"],
                    "tags": [],
                },
                "content": item["message"],
            }
            for item in messages
        ]
        try:
            semantic_scores, _ = await embedding_index.query_segment_matches(
                clean_query, buckets
            )
            semantic_available = bool(semantic_scores)
        except Exception as error:
            logger.warning("Mailbox semantic search fell back to keyword: %s", error)

    ranked = []
    total = max(1, len(messages) - 1)
    for index, item in enumerate(messages):
        keyword = _keyword_score(clean_query, item["message"])
        semantic = float(
            semantic_scores.get(f"mailbox:{item['message_id']}", {}).get("score", 0.0)
        )
        if keyword <= 0 and (not semantic_available or semantic < 0.38):
            continue
        recency = 1.0 - (index / total)
        score = (0.72 * semantic + 0.23 * keyword + 0.05 * recency)
        if not semantic_available:
            score = 0.95 * keyword + 0.05 * recency
        ranked.append(
            {
                **item,
                "match_score": round(score, 4),
                "semantic_score": round(semantic, 4) if semantic_available else None,
                "keyword_score": round(keyword, 4),
            }
        )
    ranked.sort(key=lambda item: (item["match_score"], item["message_id"]), reverse=True)
    return ranked[:safe_limit]
