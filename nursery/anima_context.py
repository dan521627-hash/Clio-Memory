"""One-way, child-safe projection of Anima's composite disposition profile.

This module deliberately accepts Anima's read model as input instead of
opening Anima stores.  It keeps the nursery from ever depending on the legacy
single ``tendency`` compatibility field or on raw adult memories.
"""

from __future__ import annotations

from typing import Any, Iterable


SCHEMA_VERSION = "anima-nursery-composite-context-v1"
MAX_SUPPORTING_TENDENCIES = 2
MAX_EMERGING_TRACES = 3
SAFE_EVENT_CATEGORIES = frozenset(
    {"care", "routine", "relationship", "environment", "celebration", "transition"}
)
RAW_ANIMA_EVENT_KEYS = frozenset(
    {"content", "context_card", "evidence", "event_summary", "raw_memory", "thought_text"}
)
FAMILY_EVENT_CATEGORIES = frozenset(
    {"care", "routine", "relationship", "environment", "celebration", "transition"}
)


def _number(value: Any) -> float:
    try:
        return round(max(0.0, min(1.0, float(value))), 3)
    except (TypeError, ValueError):
        return 0.0


def _tendency(item: Any) -> dict[str, Any] | None:
    if not isinstance(item, dict):
        return None
    label = str(item.get("label") or item.get("name") or "").strip()
    identifier = str(item.get("tendency_id") or "").strip()
    if not label or not identifier:
        return None
    return {
        "id": identifier[:160],
        "label": label[:80],
        "strength": _number(item.get("strength", item.get("score"))),
        "evidence_count": max(0, int(item.get("evidence_count") or 0)),
        "last_seen": str(item.get("last_seen") or "")[:64],
        "kind": str(item.get("kind") or "")[:80],
        "influence": "soft_context_only",
    }


def _emerging_trace(item: Any) -> dict[str, Any] | None:
    if not isinstance(item, dict):
        return None
    label = str(item.get("event_tag") or item.get("label") or "").strip()
    if not label:
        return None
    count = max(0, int(item.get("trace_count") or 0))
    if count <= 0:
        return None
    return {
        "id": f"trace:{label[:120]}",
        "label": label[:80],
        "evidence_count": count,
        "last_seen": str(item.get("last_seen") or "")[:64],
        "stage": "observing",
        "influence": "never_a_command",
    }


def build_composite_disposition_context(
    report: dict[str, Any], *, safe_evidence: Iterable[dict[str, Any]] = ()
) -> dict[str, Any]:
    """Convert Anima's composite profile into the only shape nursery may read.

    ``safe_evidence`` must already have been privacy-trimmed by Anima.  Raw
    ``report['evidence']`` is intentionally ignored because it can contain
    adult memory text and must never reach the child API.
    """

    ranked = [candidate for candidate in (_tendency(item) for item in report.get("tendencies") or []) if candidate]
    ranked.sort(
        key=lambda item: (item["strength"], item["evidence_count"], item["last_seen"]),
        reverse=True,
    )
    traces = [candidate for candidate in (_emerging_trace(item) for item in report.get("recurring_thoughts") or []) if candidate]
    traces.sort(
        key=lambda item: (item["evidence_count"], item["last_seen"]), reverse=True)
    allowed_evidence = []
    for item in safe_evidence:
        if not isinstance(item, dict):
            continue
        category = str(item.get("category") or "").strip()
        summary = str(item.get("summary") or "").strip()
        if not category or not summary:
            continue
        allowed_evidence.append(
            {
                "category": category[:80],
                "summary": summary[:160],
                "occurred_at": str(item.get("occurred_at") or "")[:64],
            }
        )
    return {
        "schema_version": SCHEMA_VERSION,
        "mode": "read_only_child_safe_summary",
        "as_of": str(report.get("as_of") or "")[:64],
        "period_days": max(1, int(report.get("days") or 30)),
        "primary_tendency": ranked[0] if ranked else None,
        "supporting_tendencies": ranked[1 : 1 + MAX_SUPPORTING_TENDENCIES],
        "emerging_traces": traces[:MAX_EMERGING_TRACES],
        "child_safe_evidence": allowed_evidence[:6],
        "rules": [
            "复合画像只影响语气、时机与表达方式。",
            "孩子当前状态、成长阶段和安全规则优先。",
            "养育室不得写入或改写 Anima 性格轨迹。",
        ],
    }


def build_child_safe_memory_event(
    event: dict[str, Any], *, disposition_report: dict[str, Any]
) -> dict[str, Any]:
    """Accept only an explicit memory-AI child-safe event, never raw Anima output."""

    if not isinstance(event, dict) or set(event) & RAW_ANIMA_EVENT_KEYS:
        raise ValueError("raw Anima event fields are not accepted by the nursery bridge")
    source_key = str(event.get("source_key") or "").strip()
    source_version = str(event.get("source_version") or "").strip()
    category = str(event.get("category") or "").strip().lower()
    summary = " ".join(str(event.get("child_safe_summary") or "").split())
    occurred_at = str(event.get("occurred_at") or "").strip()
    if not source_key or len(source_key) > 200 or not source_version or len(source_version) > 80:
        raise ValueError("child-safe event source is invalid")
    if category not in SAFE_EVENT_CATEGORIES:
        raise ValueError("child-safe event category is invalid")
    if not summary or len(summary) > 240 or not occurred_at or len(occurred_at) > 64:
        raise ValueError("child-safe event summary is invalid")
    return {
        "source_key": source_key,
        "source_version": source_version,
        "category": category,
        "summary": summary,
        "occurred_at": occurred_at,
        "disposition": build_composite_disposition_context(disposition_report),
    }


def build_child_safe_family_event(
    event: dict[str, Any], *, disposition_report: dict[str, Any]
) -> dict[str, Any]:
    """Validate the only private envelope accepted by the immediate bridge."""

    context = build_child_safe_memory_event(event, disposition_report=disposition_report)
    health_relevant = event.get("health_relevant", False)
    if health_relevant is not True and health_relevant is not False:
        raise ValueError("family-event health relevance is invalid")
    return {**context, "health_relevant": health_relevant}
