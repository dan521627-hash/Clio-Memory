"""Deterministic helpers for the approved memory upgrades; never writes data."""
from __future__ import annotations

import re
from datetime import datetime, timedelta
from difflib import SequenceMatcher

TOPIC_ALIASES = {"上班焦虑": "工作压力", "职场压力": "工作压力", "工作焦虑": "工作压力", "身体状况": "身体与健康", "健康情况": "身体与健康", "恋爱": "我们的关系", "感情关系": "我们的关系", "待办事项": "计划与待办", "任务计划": "计划与待办"}
TENDENCY_ALIASES = {"愿意靠近": "更愿意靠近", "主动靠近": "更愿意靠近", "珍惜陪伴": "更珍惜持续的陪伴", "重视陪伴": "更珍惜持续的陪伴", "爱复盘": "更习惯复盘后再行动", "先想后做": "更习惯复盘后再行动", "愿意表达": "更愿意把感受说出来", "说出感受": "更愿意把感受说出来"}
QUERY_EXPANSIONS = {"上班": ("工作", "职场"), "不舒服": ("身体不适", "生病", "健康"), "吵架": ("争执", "闹别扭", "和好"), "答应": ("决定", "承诺", "约定"), "取消": ("否决", "不做", "放弃"), "后来": ("后续", "变化", "现在")}
TENDENCY_COUNTERS = {
    "更愿意靠近": ("更倾向先安静整理自己",),
    "更倾向先安静整理自己": ("更愿意靠近", "更愿意把感受说出来"),
    "更愿意把感受说出来": ("更倾向先安静整理自己",),
    "更主动修复关系": ("更倾向先安静整理自己",),
}

def canonical_topic(value: str) -> str:
    text = " ".join(str(value or "").split())
    return TOPIC_ALIASES.get(text, text)

def canonical_tendency(value: str) -> str:
    text = " ".join(str(value or "").split())
    return TENDENCY_ALIASES.get(text, text)

def counter_tendency_labels(value: str) -> tuple[str, ...]:
    return TENDENCY_COUNTERS.get(canonical_tendency(value), ())

def expanded_query(query: str) -> str:
    """Keep the user's original words and append bounded colloquial synonyms."""
    original = " ".join(str(query or "").split())
    additions: list[str] = []
    for marker, synonyms in QUERY_EXPANSIONS.items():
        if marker in original:
            additions.extend(word for word in synonyms if word not in original)
    return " ".join([original, *dict.fromkeys(additions)]).strip()

def query_time_window(query: str, now: datetime) -> tuple[datetime, datetime] | None:
    text = str(query or "")
    day = now.replace(hour=0, minute=0, second=0, microsecond=0)
    if "昨天" in text:
        return day - timedelta(days=1), day
    if "今天" in text:
        return day, day + timedelta(days=1)
    if "上个月" in text:
        first_this = day.replace(day=1)
        last_month_day = first_this - timedelta(days=1)
        return last_month_day.replace(day=1), first_this
    match = re.search(r"(\d{4})[-年/](\d{1,2})(?:[-月/](\d{1,2})日?)?", text)
    if not match:
        return None
    year, month = int(match.group(1)), int(match.group(2))
    if match.group(3):
        start = day.replace(year=year, month=month, day=int(match.group(3)))
        return start, start + timedelta(days=1)
    start = day.replace(year=year, month=month, day=1)
    end = start.replace(year=year + 1, month=1) if month == 12 else start.replace(month=month + 1)
    return start, end

def _one_line(value: object, limit: int = 150) -> str:
    text = " ".join(str(value or "").split())
    return text[: limit - 1].rstrip() + "…" if len(text) > limit else text

def mailbox_continuity(messages: list[dict], limit: int = 6) -> str:
    """Turn the newest six handoffs into one deduplicated event narrative."""
    valid = [item for item in messages if not item.get("deleted_at") and _one_line(item.get("message"))]
    selected = valid[: max(1, min(6, int(limit)))]
    selected.reverse()
    events: list[str] = []
    for item in selected:
        raw = " ".join(str(item.get("message") or "").split())
        clauses = [part.strip(" -—：:，,。；;！!？?") for part in re.split(r"[。！？；\n]+", raw) if part.strip()]
        important = [part for part in clauses if re.search(r"后来|现在|改成|变成|决定|确定|取消|否决|不做|不要|完成|继续|等待|结果", part)]
        text = _one_line("；".join(important[-2:] if important else clauses[-1:]), 260)
        text = re.sub(
            r"^(?:(?:嗯+|然后|就是|我觉得|那个|所以说)[，,、：:\s]*)+",
            "",
            text,
        ).strip()
        if not text:
            continue
        normalized = re.sub(r"[^\u4e00-\u9fffA-Za-z0-9]", "", text).lower()
        duplicate = False
        carries_change = bool(re.search(r"后来|现在|改成|变成|不再|转为|从.+到", text))
        for old_text in events:
            old_normalized = re.sub(r"[^\u4e00-\u9fffA-Za-z0-9]", "", old_text).lower()
            if normalized == old_normalized or (
                not carries_change
                and SequenceMatcher(None, normalized, old_normalized).ratio() >= 0.78
            ):
                duplicate = True
                break
        if not duplicate:
            events.append(text)
    if not events:
        return ""
    connectors = ("最初", "随后", "之后", "后来", "接着", "目前")
    return "；".join(
        f"{connectors[min(index, len(connectors) - 1)]}，{text}"
        for index, text in enumerate(events)
    ) + "。"

def self_awareness(disposition: dict) -> str:
    composite = disposition.get("composite") or {}
    parts = []
    summary = _one_line(composite.get("summary"), 180)
    if summary and "还没有形成" not in summary:
        parts.append(summary)
    observations = list(disposition.get("observing") or composite.get("observing") or [])[:3]
    labels = [canonical_tendency(item.get("label", "")) for item in observations]
    labels = [item for item in dict.fromkeys(labels) if item]
    if labels:
        parts.append("此刻也觉察到：" + "、".join(labels) + "。")
    return "\n".join(parts)

def retrieval_reason(query: str, bucket: dict) -> str:
    meta = bucket.get("metadata") or {}
    haystack = " ".join([str(meta.get("name") or ""), *map(str, meta.get("domain") or []), *map(str, meta.get("tags") or [])])
    hits = [token for token in re.findall(r"[\u4e00-\u9fff]{2,}", str(query or "")) if token in haystack]
    if hits:
        return "主题或人物相同：" + "、".join(dict.fromkeys(hits[:3]))
    if bucket.get("matched_segment"):
        return "原文片段语义相关"
    if bucket.get("bm25_score"):
        return "正文关键词相关"
    return "与原问题及其口语近义表达相关"
