"""Read-only calendar projection across memory sidecars."""

from __future__ import annotations

from memory_segments import split_memory_segments
from utils import normalize_beijing_timestamp


KIND_LABELS = {
    "memory_segment": "记忆",
    "memory_created": "新记忆桶",
    "memory_updated": "记忆更新",
    "memory_trigger": "前瞻提醒",
    "mailbox": "信箱",
    "thought": "心念",
    "darkflow": "暗涌",
    "behavior": "静默表达",
    "task": "未竟事项",
    "treasury": "小金库",
    "fact": "事实时间线",
}


def _calendar_time(value: str) -> str:
    raw = str(value or "").strip()
    if not raw:
        return ""
    try:
        return normalize_beijing_timestamp(raw)
    except (TypeError, ValueError):
        return raw


def _summary(value: str, limit: int = 180) -> str:
    text = " ".join(str(value or "").split())
    return text if len(text) <= limit else text[:limit].rstrip() + "..."


def _on_date(value: str, target: str) -> bool:
    return str(value or "").strip()[:10] == target


def build_calendar_day(
    target: str,
    *,
    buckets: list[dict] | None = None,
    mailbox: list[dict] | None = None,
    behaviors: list[dict] | None = None,
    tasks: list[dict] | None = None,
    treasury: list[dict] | None = None,
    thoughts: list[dict] | None = None,
    darkflow: dict | None = None,
    facts: list[dict] | None = None,
    include_archived: bool = True,
    include_sealed: bool = True,
) -> dict:
    """Build one chronological day without writing or copying source records."""
    entries: list[dict] = []

    for bucket in buckets or []:
        metadata = bucket.get("metadata") or {}
        archived = str(metadata.get("type", "")).lower() == "archived"
        sealed = bool(metadata.get("sealed", False))
        if (archived and not include_archived) or (sealed and not include_sealed):
            continue
        bucket_id = str(bucket.get("id", ""))
        title = str(metadata.get("name") or bucket_id or "未命名记忆")
        created = str(metadata.get("created", ""))
        segments = split_memory_segments(bucket.get("content", ""), created)
        matching_segments = [
            segment for segment in segments if _on_date(segment.get("timestamp"), target)
        ]
        for segment in matching_segments:
            entries.append(
                {
                    "kind": "memory_segment",
                    "id": bucket_id,
                    "segment_id": segment.get("segment_id"),
                    "title": title,
                    "time": _calendar_time(segment.get("timestamp") or target),
                    "note": _summary(segment.get("content", "")),
                }
            )
        metadata_dates = {
            str(created)[:10]: "memory_created",
            str(metadata.get("last_active", ""))[:10]: "memory_updated",
            str(metadata.get("trigger_date", ""))[:10]: "memory_trigger",
        }
        metadata_kind = metadata_dates.get(target)
        if metadata_kind and not matching_segments:
            entries.append(
                {
                    "kind": metadata_kind,
                    "id": bucket_id,
                    "title": title,
                    "time": _calendar_time(metadata.get("last_active") or created or target),
                    "note": _summary(segments[-1].get("content", "")),
                }
            )

    for item in mailbox or []:
        if _on_date(item.get("created_at"), target):
            entries.append(
                {
                    "kind": "mailbox",
                    "id": item.get("message_id"),
                    "title": f"信箱 #{item.get('message_id')}",
                    "time": _calendar_time(item.get("created_at")),
                    "note": _summary(item.get("message", ""), 160),
                }
            )

    for item in behaviors or []:
        context = item.get("context") or {}
        if context.get("phase") == "silence" or item.get("action_type") == "silence_nudge":
            continue
        timestamp = item.get("delivered_at") or item.get("decided_at") or ""
        if _on_date(timestamp, target):
            entries.append(
                {
                    "kind": "behavior",
                    "id": item.get("action_id"),
                    "title": "一次静默表达",
                    "time": _calendar_time(timestamp),
                    "note": _summary(item.get("content", ""), 160),
                }
            )

    for item in tasks or []:
        timestamp = item.get("updated_at") or item.get("created_at") or ""
        if _on_date(timestamp, target):
            entries.append(
                {
                    "kind": "task",
                    "id": item.get("task_id"),
                    "title": item.get("title") or "未竟事项",
                    "time": _calendar_time(timestamp),
                    "note": str(item.get("status", "open")),
                }
            )

    for item in treasury or []:
        timestamp = item.get("occurred_at") or item.get("created_at") or ""
        if _on_date(timestamp, target):
            entry_type = "收入" if item.get("entry_type") == "income" else "支出"
            amount = item.get("amount") or item.get("amount_yuan") or ""
            entries.append(
                {
                    "kind": "treasury",
                    "id": item.get("entry_id"),
                    "title": f"{entry_type} {amount}".strip(),
                    "time": _calendar_time(timestamp),
                    "note": _summary(item.get("reason", ""), 160),
                }
            )

    seen_thoughts: set[str] = set()
    for item in thoughts or []:
        key = str(item.get("canonical_tag") or item.get("thought_text") or "")
        if not key or key in seen_thoughts:
            continue
        seen_thoughts.add(key)
        thought_times = [
            item.get("last_seen"),
            item.get("updated_at"),
            item.get("first_seen"),
        ]
        timestamp = next(
            (value for value in thought_times if _on_date(value, target)),
            item.get("first_seen") or item.get("last_seen") or item.get("updated_at"),
        )
        if _on_date(timestamp, target):
            entries.append(
                {
                    "kind": "thought",
                    "id": key,
                    "title": "一闪而过" if item.get("status") == "flash" else "反复萦绕",
                    "time": _calendar_time(timestamp),
                    "note": _summary(item.get("thought_text") or item.get("event_tag"), 160),
                }
            )

    if darkflow and _on_date(darkflow.get("created_at"), target):
        entries.append(
            {
                "kind": "darkflow",
                "id": darkflow.get("cycle_id"),
                "title": "一封暗涌",
                "time": _calendar_time(darkflow.get("created_at")),
                "note": _summary(darkflow.get("content", ""), 180),
            }
        )

    for group in facts or []:
        for version in group.get("versions") or []:
            if _on_date(version.get("effective_date"), target):
                entries.append(
                    {
                        "kind": "fact",
                        "id": version.get("version_id"),
                        "title": group.get("fact_label") or "一项事实",
                        "time": _calendar_time(version.get("effective_date")),
                        "note": _summary(version.get("fact_value", ""), 160),
                        "bucket_id": version.get("source_bucket_id", ""),
                    }
                )

    entries.sort(key=lambda item: str(item.get("time", "")), reverse=True)
    return {"date": target, "items": entries, "count": len(entries)}


def format_calendar_day(day: dict) -> str:
    if not day.get("items"):
        return f"【{day.get('date', '')}】这一天没有可见记录。"
    lines = [f"【{day.get('date', '')}】共 {day.get('count', 0)} 条："]
    for item in day.get("items", []):
        stamp = str(item.get("time") or "")
        clock = stamp[11:16] if len(stamp) >= 16 else ""
        identity = (
            f"bucket_id={item['id']}"
            if str(item.get("kind", "")).startswith("memory")
            else f"id={item.get('id')}"
        )
        lines.append(
            f"- {clock or '全天'}｜{KIND_LABELS.get(item.get('kind'), item.get('kind'))}｜"
            f"{item.get('title')}｜{identity}\n  {item.get('note', '')}"
        )
    return "\n".join(lines)
