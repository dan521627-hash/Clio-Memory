# ============================================================
# Module: MCP Server Entry Point (server.py)
# 模块：MCP 服务器主入口
#
# Starts the Ombre Brain MCP service and registers memory
# operation tools for Claude to call.
# 启动 Ombre Brain MCP 服务，注册记忆操作工具供 Claude 调用。
#
# Core responsibilities:
# 核心职责：
#   - Initialize config, bucket manager, dehydrator, decay engine
#     初始化配置、记忆桶管理器、脱水器、衰减引擎
#   - Expose MCP tools:
#     暴露 MCP 工具：
#       breath — Surface unresolved memories or search by keyword
#                浮现未解决记忆 或 按关键词检索
#       hold   — Store a single memory
#                存储单条记忆
#       grow   — Diary digest, auto-split into multiple buckets
#                日记归档，自动拆分多桶
#       mailbox — Read append-only handoff messages
#                查询窗口接力留言
#       trace  — Modify metadata / resolved / delete
#                修改元数据 / resolved 标记 / 删除
#       pulse_boot — Compact startup context
#                开机专用摘要
#       pulse  — System status + bucket listing
#                系统状态 + 所有桶列表
#       digest_preview — Read-only automatic digestion rehearsal
#                自动消化只读演习报告
#       timeline — Read or confirm dated fact versions
#                查询或确认事实的新旧时间线
#       recall — Read one bucket's exact original text by ID
#                按桶 ID 读取完整原文
#       split_bucket — Copy selected source ranges into a new child bucket
#                按时间或标记复制原文到新子桶
#       feedback — Rate one result from a specific retrieval
#                对一次检索中的结果反馈有用或不相关
#       treasury — AI income, expenses, balance, and ledger history
#                AI 小金库收支、余额与账目历史
#       xinchao_status — Read the current emotional sidecar state
#                只读查看当前心潮状态
#       inner_state — Inspect private thoughts, resonance, and tension
#                只读查看心念、记忆共振与内在张力
#
# Startup:
# 启动方式：
#   Local:  python server.py
#   Remote: OMBRE_TRANSPORT=streamable-http python server.py
#   Docker: docker-compose up
# ============================================================

import os
import sys
import random
import logging
import asyncio
import json
import hashlib
import time
import httpx
import re
from contextlib import asynccontextmanager, suppress
from datetime import date, datetime, timedelta, timezone
from typing import Optional

# --- Ensure same-directory modules can be imported ---
# --- 确保同目录下的模块能被正确导入 ---
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from mcp.server.fastmcp import FastMCP

from bucket_manager import BucketManager
from behavior_service import BehaviorService
from calendar_view import build_calendar_day, format_calendar_day
from conflict_detector import ConflictDetector
from dehydrator import Dehydrator
from decay_engine import DecayEngine
from digestion_planner import DigestionPlanner
from fact_timeline_service import FactTimelineService
from fact_timeline_store import FactTimelineStore
from history_retention import HistoryRetentionEngine
from mailbox_store import MailboxStore
from mailbox_search import search_mailbox
from memory_segments import split_memory_segments
from relation_store import RelationStore
from request_diagnostics import (
    MCPRequestDiagnosticsMiddleware,
    current_mcp_event_id,
    current_mcp_session_id,
)
from tag_policy import normalize_analysis, parse_category
from task_service import TaskService
from topic_store import TOPIC_TREE, TopicStore, validate_topic
from treasury_store import TreasuryStore
from utils import beijing_now, load_config, setup_logging
from xinchao_store import XinchaoService

# --- Load config & init logging / 加载配置 & 初始化日志 ---
config = load_config()
setup_logging(config.get("log_level", "INFO"))
logger = logging.getLogger("ombre_brain")

RESPONSE_SEAL_ENV = "OMBRE_RESPONSE_SEAL"
RESPONSE_SEAL_MISSING = "[ERROR: OMBRE_RESPONSE_SEAL_NOT_CONFIGURED]"
RESPONSE_SEAL_PLACEHOLDERS = {
    "CHANGE_ME_TO_A_RANDOM_PRIVATE_SEAL",
    "CHANGE_ME",
    "YOUR_PRIVATE_SEAL",
}
PAGINATION_MIN_PAGE_SIZE = 200
PAGINATION_MAX_PAGE_SIZE = 8000
PAGINATION_SNAPSHOT_TTL_SECONDS = 600
PAGINATION_SNAPSHOT_LIMIT = 64
_PAGINATION_SNAPSHOTS: dict[str, tuple[float, str, str]] = {}
_TIMESTAMP_SEGMENT_RE = re.compile(
    r"(?m)^--- (?P<timestamp>\d{4}-\d{2}-\d{2}T\d{2}:\d{2}) ---\r?$"
)


def _with_response_seal(text: str) -> str:
    """Append the response seal without caching or persisting its value."""
    seal = os.environ.get(RESPONSE_SEAL_ENV, "").strip() or RESPONSE_SEAL_MISSING
    return f"{text}\nseal: {seal}"


def _validate_response_seal() -> str:
    """Refuse production startup with a missing or documented example seal."""
    seal = os.environ.get(RESPONSE_SEAL_ENV, "").strip()
    if not seal:
        raise RuntimeError(
            "OMBRE_RESPONSE_SEAL 未配置。请先在 .env 中生成随机私密暗语。"
        )
    if seal.upper() in {value.upper() for value in RESPONSE_SEAL_PLACEHOLDERS}:
        raise RuntimeError(
            "OMBRE_RESPONSE_SEAL 仍是示例值。请换成随机私密暗语后再启动。"
        )
    return seal


def _format_treasury_summary(summary: dict) -> str:
    symbol = summary.get("symbol", "¥")
    return (
        f"当前总金额: {symbol}{summary.get('balance', '0.00')}\n"
        f"累计总收入: {symbol}{summary.get('total_income', '0.00')}\n"
        f"累计总支出: {symbol}{summary.get('total_expense', '0.00')}"
    )


def _split_response_pages(text: str, page_size: int) -> list[str]:
    """Split text without loss, preferring line boundaries when practical."""
    if not text:
        return [""]
    pages = []
    start = 0
    while start < len(text):
        end = min(len(text), start + page_size)
        if end < len(text):
            earliest_break = start + max(1, page_size // 2)
            line_break = text.rfind("\n", earliest_break, end)
            if line_break >= earliest_break:
                end = line_break + 1
        pages.append(text[start:end])
        start = end
    return pages


def _store_pagination_snapshot(text: str, scope: str) -> str:
    now = time.monotonic()
    expired = [
        key
        for key, (created_at, _, _) in _PAGINATION_SNAPSHOTS.items()
        if now - created_at > PAGINATION_SNAPSHOT_TTL_SECONDS
    ]
    for key in expired:
        _PAGINATION_SNAPSHOTS.pop(key, None)
    while len(_PAGINATION_SNAPSHOTS) >= PAGINATION_SNAPSHOT_LIMIT:
        oldest = min(_PAGINATION_SNAPSHOTS, key=lambda key: _PAGINATION_SNAPSHOTS[key][0])
        _PAGINATION_SNAPSHOTS.pop(oldest, None)

    content_id = hashlib.sha256(f"{scope}\0{text}".encode("utf-8")).hexdigest()[:12]
    _PAGINATION_SNAPSHOTS[content_id] = (now, scope, text)
    return content_id


def _load_pagination_snapshot(content_id: str, scope: str) -> str | None:
    item = _PAGINATION_SNAPSHOTS.get(content_id)
    if not item:
        return None
    created_at, stored_scope, text = item
    if time.monotonic() - created_at > PAGINATION_SNAPSHOT_TTL_SECONDS:
        _PAGINATION_SNAPSHOTS.pop(content_id, None)
        return None
    if stored_scope != scope:
        return None
    return text


def _paginate_response(
    text: str,
    page: int,
    page_size: int,
    next_call_template: str,
    content_id: str = "",
    snapshot_scope: str = "",
) -> str:
    """Return a sealed legacy response or one bounded, verifiable page."""
    if page < 1:
        return _with_response_seal("page 必须从 1 开始。")
    if page_size == 0:
        if content_id:
            return _with_response_seal("使用 content_id 继续翻页时，必须同时提供 page_size。")
        if page != 1:
            return _with_response_seal("读取第 2 页及以后时，必须同时提供 page_size。")
        return _with_response_seal(text)
    if not PAGINATION_MIN_PAGE_SIZE <= page_size <= PAGINATION_MAX_PAGE_SIZE:
        return _with_response_seal(
            f"page_size 必须是 0，或 {PAGINATION_MIN_PAGE_SIZE}-"
            f"{PAGINATION_MAX_PAGE_SIZE} 之间的整数。"
        )

    if content_id:
        snapshot = _load_pagination_snapshot(content_id, snapshot_scope)
        if snapshot is None:
            return _with_response_seal(
                "分页内容已过期或内容编号不匹配，请从第 1 页重新读取。"
            )
        text = snapshot
    elif page > 1:
        return _with_response_seal(
            "继续读取第 2 页及以后时，必须带上上一页返回的 content_id。"
        )
    else:
        content_id = _store_pagination_snapshot(text, snapshot_scope)

    pages = _split_response_pages(text, page_size)
    total_pages = len(pages)
    if page > total_pages:
        return _with_response_seal(
            f"请求的第 {page} 页不存在；这份内容一共 {total_pages} 页。"
        )

    chunk = pages[page - 1]
    has_next = page < total_pages
    if has_next:
        continuation = next_call_template.format(
            page=page + 1, content_id=content_id
        )
    else:
        continuation = "已到最后一页，无需继续调用。"
    header = (
        "=== 分页信息 ===\n"
        f"当前页: {page}/{total_pages}\n"
        f"本页字数: {len(chunk)}\n"
        f"全文字数: {len(text)}\n"
        f"内容编号: {content_id}\n"
        f"还有下一页: {'是' if has_next else '否'}\n"
        f"下一步: {continuation}\n"
        "=== 本页内容 ===\n"
    )
    return _with_response_seal(header + chunk)


def _split_timestamped_segments(text: str, created_at: str = "") -> list[dict]:
    """Split source into lossless slices at append timestamp marker lines."""
    return [
        {**segment, "text": segment["raw_text"]}
        for segment in split_memory_segments(text, created_at)
    ]


def _visible_bucket_segment(bucket: dict) -> dict:
    matched = bucket.get("matched_segment")
    if matched:
        return matched
    metadata = bucket.get("metadata", {})
    segments = split_memory_segments(
        bucket.get("content", ""), metadata.get("created", "")
    )
    return {**segments[-1], "total_segments": len(segments)}


async def _dehydrate_visible_segment(bucket: dict) -> str:
    segment = _visible_bucket_segment(bucket)
    metadata = dict(bucket.get("metadata", {}))
    metadata["id"] = (
        f"{bucket.get('id', metadata.get('id', 'unknown'))}:"
        f"segment:{segment.get('source_index', 1)}"
    )
    summary = await dehydrator.dehydrate(segment.get("content", ""), metadata)
    label = segment.get("timestamp") or "初始正文"
    total = segment.get("total_segments", 1)
    return (
        f"[记忆包 {segment.get('source_index', 1)}/{total} | {label}]\n"
        f"{summary}"
    )


def _paginate_recall_segments(
    payload: dict,
    page: int,
    segments_per_page: int,
    newest_first: bool,
    content_id: str,
    snapshot_scope: str,
    feedback_note: str = "",
) -> str:
    if page < 1:
        return _with_response_seal("page 必须从 1 开始。")
    if not 1 <= segments_per_page <= 20:
        return _with_response_seal("segments_per_page 必须是 1-20 之间的整数。")

    if content_id:
        snapshot = _load_pagination_snapshot(content_id, snapshot_scope)
        if snapshot is None:
            return _with_response_seal(
                "分段内容已过期或内容编号不匹配，请从第 1 页重新读取。"
            )
        try:
            payload = json.loads(snapshot)
        except (TypeError, ValueError):
            return _with_response_seal("分段快照损坏，请从第 1 页重新读取。")
    elif page > 1:
        return _with_response_seal(
            "继续读取第 2 页及以后时，必须带上上一页返回的 content_id。"
        )
    else:
        snapshot = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        content_id = _store_pagination_snapshot(snapshot, snapshot_scope)

    segments = _split_timestamped_segments(
        payload.get("content", ""), payload.get("created_at", "")
    )
    ordered = list(reversed(segments)) if newest_first else segments
    total_pages = max(1, (len(ordered) + segments_per_page - 1) // segments_per_page)
    if page > total_pages:
        return _with_response_seal(
            f"请求的第 {page} 页不存在；这份内容一共 {total_pages} 页。"
        )

    start = (page - 1) * segments_per_page
    selected = ordered[start : start + segments_per_page]
    rendered = []
    for segment in selected:
        timestamp_label = segment["timestamp"] or "初始正文"
        rendered.append(
            f"[原文段 {segment['source_index']}/{len(segments)} | {timestamp_label}]\n"
            f"{segment['text']}"
        )

    has_next = page < total_pages
    if has_next:
        continuation = (
            f"recall(bucket_id={json.dumps(payload['bucket_id'], ensure_ascii=False)}, "
            f"include_sealed={str(bool(payload.get('include_sealed'))).lower()}, "
            f"page={page + 1}, segments_per_page={segments_per_page}, "
            f"newest_first={str(newest_first).lower()}, content_id=\"{content_id}\")"
        )
    else:
        continuation = "已到最早一页，无需继续调用。" if newest_first else "已到最后一页，无需继续调用。"
    header = (
        "=== 分段读取 ===\n"
        f"bucket_id: {payload['bucket_id']}\n"
        f"名称: {payload.get('name', payload['bucket_id'])}\n"
        f"类型: {payload.get('bucket_type', 'unknown')}\n"
        f"当前页: {page}/{total_pages}\n"
        f"本页段数: {len(selected)}\n"
        f"总段数: {len(segments)}\n"
        f"读取顺序: {'最新到最早' if newest_first else '最早到最新'}\n"
        f"内容编号: {content_id}\n"
        f"还有下一页: {'是' if has_next else '否'}\n"
        f"下一步: {continuation}\n"
        f"{feedback_note}"
        "=== 本页原文 ===\n"
    )
    return _with_response_seal(header + "\n\n".join(rendered))


def _recall_segment_cursor(
    payload: dict,
    limit: int = 1,
    before_id: str = "",
    feedback_note: str = "",
) -> str:
    """Read a bounded number of exact packets, newest first."""
    if not 1 <= limit <= 20:
        return _with_response_seal("limit 必须是 1-20 之间的整数。")

    segments = _split_timestamped_segments(
        payload.get("content", ""), payload.get("created_at", "")
    )
    ordered = list(reversed(segments))
    cursor = str(before_id or "").strip()
    cursor_offset = 0
    if cursor:
        positions = [
            index for index, segment in enumerate(ordered)
            if segment.get("segment_id") == cursor
        ]
        if not positions:
            return _with_response_seal(
                "before_id 不属于这个桶，请先不带 before_id 读取最新一包。"
            )
        cursor_offset = positions[0] + 1
        ordered = ordered[cursor_offset:]

    selected = ordered[:limit]
    if not selected:
        return _with_response_seal("已经读到这个桶最早的一包，没有更早内容。")

    rendered = []
    for segment in selected:
        timestamp_label = segment.get("timestamp") or "初始正文"
        rendered.append(
            f"[记忆包 {segment['segment_id']} | "
            f"{segment['source_index']}/{len(segments)} | {timestamp_label}]\n"
            f"{segment['text']}"
        )

    consumed = cursor_offset + len(selected)
    has_older = consumed < len(segments)
    next_before_id = selected[-1]["segment_id"]
    if has_older:
        continuation = (
            f"recall(bucket_id={json.dumps(payload['bucket_id'], ensure_ascii=False)}, "
            f"include_sealed={str(bool(payload.get('include_sealed'))).lower()}, "
            f"limit={limit}, before_id=\"{next_before_id}\")"
        )
    else:
        continuation = "已到最早一包，无需继续调用。"

    header = (
        "=== 最新记忆包 ===\n"
        f"bucket_id: {payload['bucket_id']}\n"
        f"名称: {payload.get('name', payload['bucket_id'])}\n"
        f"本次返回: {len(selected)} 包\n"
        f"总包数: {len(segments)}\n"
        f"当前 before_id: {next_before_id}\n"
        f"还有更早内容: {'是' if has_older else '否'}\n"
        f"继续往前: {continuation}\n"
        f"{feedback_note}"
        "=== 本次原文 ===\n"
    )
    return _with_response_seal(header + "\n\n".join(rendered))


def _pin_level(metadata: dict) -> str:
    """Treat legacy pinned buckets as core without rewriting their metadata."""
    if metadata.get("protected", False):
        return "core"
    if not metadata.get("pinned", False):
        return ""
    level = str(metadata.get("pin_level", "")).strip().lower()
    return "important" if level == "important" else "core"


def _pulse_boot_sort_order(bucket: dict) -> int:
    """Return a safe, user-controlled startup order for a bucket."""
    try:
        return int(bucket.get("metadata", {}).get("sort_order", 0))
    except (TypeError, ValueError):
        return 0


_PULSE_BOOT_TIMESTAMP_LINE_RE = re.compile(
    r"^---\s*\d{4}-\d{2}-\d{2}(?:[T ][^\r\n-]+)?\s*-{2,}$"
)
_PULSE_BOOT_SENTENCE_END_RE = re.compile(r"[。！？!?](?:[\"'”’）】》]*)")


def _pulse_boot_core_lead(bucket: dict, max_chars: int = 80) -> str:
    """Return one original opening sentence without summarizing the bucket."""
    metadata = bucket.get("metadata", {})
    name = str(metadata.get("name", "")).strip()
    candidates = []
    for raw_line in str(bucket.get("content", "")).splitlines():
        line = " ".join(raw_line.strip().split())
        if not line or _PULSE_BOOT_TIMESTAMP_LINE_RE.fullmatch(line):
            continue
        plain = line.lstrip("#").strip()
        if not plain or plain == name or plain == "这是整个桶的序言":
            continue
        if plain.startswith("【") and plain.endswith("】") and len(plain) <= 60:
            continue
        candidates.append(plain)

    text = " ".join(candidates)
    if not text:
        return ""
    sentence_end = _PULSE_BOOT_SENTENCE_END_RE.search(text)
    if sentence_end:
        text = text[: sentence_end.end()].strip()
    if len(text) > max_chars:
        text = text[: max(1, max_chars - 1)].rstrip() + "…"
    return text


_PULSE_BOOT_TODO_META_RE = re.compile(r"待办|TODO|未完成|未完结", re.IGNORECASE)
_PULSE_BOOT_TODO_BODY_RE = re.compile(
    r"(?im)^\s*(?:[-*]\s*)?(?:\[ \]\s*)?"
    r"(?:待办|TODO|未完成|未完结|需要确认)\s*[:：]?|\-\s*\[ \]"
)
_PULSE_BOOT_DIALOGUE_RE = re.compile(
    r"对话|会话|窗口|聊天|conversation|session", re.IGNORECASE
)


def _pulse_boot_time_key(bucket: dict) -> str:
    metadata = bucket.get("metadata", {})
    return metadata.get("last_active") or metadata.get("created") or ""


def _pulse_boot_hormone_summary(text: str) -> str:
    """Keep readable state while hiding timer and transport bookkeeping."""
    hidden_prefixes = ("离开计时起点", "状态时间")
    return "\n".join(
        line
        for line in str(text or "").splitlines()
        if not line.strip().startswith(hidden_prefixes)
    ).strip()


def _is_pulse_boot_dialogue_archive(bucket: dict) -> bool:
    metadata = bucket.get("metadata", {})
    if metadata.get("type") != "archived":
        return False
    searchable = " ".join(
        [
            str(metadata.get("name", "")),
            *map(str, metadata.get("domain", [])),
            *map(str, metadata.get("tags", [])),
        ]
    )
    return bool(_PULSE_BOOT_DIALOGUE_RE.search(searchable))


def _is_pulse_boot_todo(bucket: dict) -> bool:
    metadata = bucket.get("metadata", {})
    if metadata.get("resolved", False):
        return False
    metadata_text = " ".join(
        [
            *map(str, metadata.get("domain", [])),
            *map(str, metadata.get("tags", [])),
        ]
    )
    return bool(
        _PULSE_BOOT_TODO_META_RE.search(metadata_text)
        or _PULSE_BOOT_TODO_BODY_RE.search(bucket.get("content", ""))
    )


def _clip_pulse_boot_bucket(bucket: dict, summary: str, limit: int) -> str:
    bucket_id = bucket.get("id", "unknown")
    name = bucket.get("metadata", {}).get("name", bucket_id)
    prefix = f"bucket_id: {bucket_id}\n"
    fallback = f"📌 记忆桶: {name}"
    safe_limit = max(len(prefix) + len(fallback), int(limit))
    available = safe_limit - len(prefix)
    text = summary.strip() or fallback
    if len(text) > available:
        text = text[: max(1, available - 1)].rstrip() + "…"
    return prefix + text


def _render_pulse_boot_items(
    buckets: list[dict], summaries: dict[str, str], budget: int, per_item_max: int
) -> str:
    if not buckets:
        return ""
    separator = "\n---\n"
    usable = max(0, int(budget) - len(separator) * (len(buckets) - 1))
    item_limit = max(80, min(int(per_item_max), usable // len(buckets)))
    return separator.join(
        _clip_pulse_boot_bucket(
            bucket,
            summaries.get(bucket["id"], "（摘要暂不可用）"),
            item_limit,
        )
        for bucket in buckets
    )


async def _pulse_boot_mailbox_context() -> dict | None:
    try:
        messages = await mailbox_store.list(limit=100)
    except Exception as error:
        logger.warning("pulse_boot mailbox read failed: %s", error)
        return None
    latest = next(
        (
            item
            for item in messages
            if item.get("source_tool") != "xinchao_settlement"
        ),
        None,
    )
    if not latest:
        return None
    try:
        related = await search_mailbox(
            mailbox_store,
            bucket_mgr.embedding_index,
            latest.get("message", ""),
            limit=3,
            include_deleted=False,
            exclude_ids={int(latest["message_id"])},
        )
    except Exception as error:
        logger.warning("pulse_boot related mailbox search failed: %s", error)
        related = []
    return {
        **latest,
        "related_messages": [
            {
                "message_id": item["message_id"],
                "created_at": item["created_at"],
                "message": item["message"],
                "match_score": item.get("match_score"),
            }
            for item in related
        ],
    }


_MAILBOX_CONTEXT_UNSET = object()


async def _pulse_boot_mailbox_section(latest=_MAILBOX_CONTEXT_UNSET) -> str:
    if latest is _MAILBOX_CONTEXT_UNSET:
        try:
            latest = await mailbox_store.latest()
        except Exception as error:
            logger.warning("pulse_boot mailbox read failed: %s", error)
            return ""
    if not latest:
        return ""
    preview_limit = max(
        100, int(config.get("mailbox", {}).get("pulse_preview_chars", 1000))
    )
    message = latest["message"]
    suffix = ""
    if len(message) > preview_limit:
        message = message[:preview_limit].rstrip() + "…"
        suffix = "\n（留言较长，请使用 mailbox() 查看全文）"
    return (
        f"message_id: {latest['message_id']}\n"
        f"时间: {latest['created_at']}\n{message}{suffix}"
    )


def _parse_bucket_created(value: str) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _eligible_pulse_boot_feelings(
    buckets: list[dict], min_age_days: int, now: datetime | None = None
) -> list[dict]:
    reference = now or datetime.now(timezone.utc)
    if reference.tzinfo is None:
        reference = reference.replace(tzinfo=timezone.utc)
    cutoff = reference.astimezone(timezone.utc) - timedelta(
        days=max(0, int(min_age_days))
    )
    eligible = []
    for bucket in buckets:
        metadata = bucket.get("metadata", {})
        created = _parse_bucket_created(metadata.get("created", ""))
        if (
            metadata.get("ai_feeling", False)
            and not metadata.get("sealed", False)
            and not metadata.get("pinned", False)
            and not metadata.get("protected", False)
            and created is not None
            and created <= cutoff
        ):
            eligible.append(bucket)
    return eligible


def _render_pulse_boot_feeling(
    bucket: dict | None,
    summary: str,
    min_age_days: int,
    limit: int,
) -> str:
    if not bucket:
        return f"暂无存在超过 {min_age_days} 天的感受类记忆。"
    metadata = bucket.get("metadata", {})
    created = _parse_bucket_created(metadata.get("created", ""))
    recorded_on = created.date().isoformat() if created else "日期未知"
    bucket_id = bucket.get("id", "unknown")
    prefix = f"bucket_id: {bucket_id}\n记录日期: {recorded_on}\n"
    safe_limit = max(len(prefix) + 40, int(limit))
    available = safe_limit - len(prefix)
    text = summary.strip() or f"📌 记忆桶: {metadata.get('name', bucket_id)}"
    if len(text) > available:
        text = text[: max(1, available - 1)].rstrip() + "…"
    return prefix + text


def _normalize_trigger_date(value: str) -> str:
    text = str(value).strip()
    try:
        parsed = date.fromisoformat(text)
    except (TypeError, ValueError):
        raise ValueError("触发日期必须是有效的 YYYY-MM-DD。") from None
    if parsed.isoformat() != text:
        raise ValueError("触发日期必须是有效的 YYYY-MM-DD。")
    return text


def _prospective_today(now: datetime | None = None) -> date:
    return beijing_now(now).date()


def _with_server_write_date(content: str) -> str:
    """Prefix every hold payload with the server's current configured date."""
    today = _prospective_today()
    prefix = f"【{today.isoformat()}】"
    text = str(content or "")
    if text.startswith(prefix):
        return text
    return f"{prefix}\n{text}"


def _due_prospective_buckets(
    buckets: list[dict], today: date
) -> list[dict]:
    due = []
    for bucket in buckets:
        metadata = bucket.get("metadata", {})
        if metadata.get("sealed", False) or metadata.get(
            "trigger_processed", False
        ):
            continue
        try:
            trigger = date.fromisoformat(str(metadata.get("trigger_date", "")))
        except ValueError:
            continue
        if trigger <= today:
            item = dict(bucket)
            item["trigger_date_value"] = trigger
            due.append(item)
    due.sort(
        key=lambda item: (
            0 if item["trigger_date_value"] == today else 1,
            item["trigger_date_value"],
            item["id"],
        )
    )
    return due


def _render_prospective_item(
    bucket: dict, summary: str, today: date, limit: int
) -> str:
    trigger = bucket["trigger_date_value"]
    overdue_days = (today - trigger).days
    status = "[今天到期]" if overdue_days == 0 else f"[逾期 {overdue_days} 天]"
    bucket_id = bucket.get("id", "unknown")
    prefix = (
        f"bucket_id: {bucket_id}\n{status}\n"
        f"触发日期: {trigger.isoformat()}\n"
    )
    metadata = bucket.get("metadata", {})
    text = summary.strip() or f"📌 记忆桶: {metadata.get('name', bucket_id)}"
    safe_limit = max(len(prefix) + 40, int(limit))
    available = safe_limit - len(prefix)
    if len(text) > available:
        text = text[: max(1, available - 1)].rstrip() + "…"
    return prefix + text


def _render_prospective_items(
    buckets: list[dict],
    summaries: dict[str, str],
    today: date,
    budget: int,
    per_item_max: int,
) -> str:
    if not buckets:
        return ""
    separator = "\n---\n"
    usable = max(0, int(budget) - len(separator) * (len(buckets) - 1))
    item_limit = max(120, min(int(per_item_max), usable // len(buckets)))
    return separator.join(
        _render_prospective_item(
            bucket,
            summaries.get(bucket["id"], "（摘要暂不可用）"),
            today,
            item_limit,
        )
        for bucket in buckets
    )

# --- Initialize three core components / 初始化三大核心组件 ---
bucket_mgr = BucketManager(config)                  # Bucket manager / 记忆桶管理器
dehydrator = Dehydrator(config)                      # Dehydrator / 脱水器
decay_engine = DecayEngine(config, bucket_mgr)       # Decay engine / 衰减引擎
digestion_planner = DigestionPlanner(config, bucket_mgr)
conflict_detector = ConflictDetector(config, bucket_mgr, dehydrator)
mailbox_store = MailboxStore(config)
relation_store = RelationStore(config)
fact_timeline_store = FactTimelineStore(config)
treasury_store = TreasuryStore(config)
xinchao_service = XinchaoService(config)
behavior_service = BehaviorService(config, xinchao_service.evaluator)
task_service = TaskService(config, xinchao_service.evaluator, bucket_mgr.embedding_index)
fact_timeline_service = FactTimelineService(
    config, xinchao_service.evaluator, fact_timeline_store, bucket_mgr
)
topic_store = TopicStore(config)
retrieval_feedback_store = bucket_mgr.retrieval_feedback
history_retention_engine = HistoryRetentionEngine(
    config, bucket_mgr, bucket_mgr.history_store
)


async def _auto_topic_new_bucket(bucket_id: str) -> dict:
    """Place only newly created buckets; existing buckets require user approval."""
    try:
        bucket = await bucket_mgr.get(bucket_id)
        if not bucket:
            return {"status": "missing"}
        metadata = bucket.get("metadata", {})
        return await topic_store.auto_assign(
            bucket_id,
            str(metadata.get("name") or bucket_id),
            str(bucket.get("content", "")),
            metadata,
        )
    except Exception as error:
        logger.warning("Topic auto-assignment failed for %s: %s", bucket_id, error)
        return {"status": "unassigned", "error": str(error)}


async def _xinchao_memory_resonance_provider(
    state: dict, event_contexts: list[dict]
) -> list[dict]:
    """Retrieve a few relevant bucket or mailbox snippets without changing data."""
    settings = config.get("xinchao", {})
    if not bool(settings.get("memory_resonance_enabled", True)):
        return []
    max_items = max(1, min(4, int(settings.get("memory_resonance_max_items", 3))))
    mailbox_enabled = bool(
        settings.get("memory_resonance_mailbox_enabled", True)
    )
    threshold = max(
        0.0, min(1.0, float(settings.get("memory_resonance_threshold", 0.68)))
    )
    context_text = " ".join(
        str(item.get("context_card") or item.get("event_summary") or "")
        for item in event_contexts[-3:]
    ).strip()
    strongest = sorted(
        (state.get("pipes") or {}).items(),
        key=lambda item: float(item[1]),
        reverse=True,
    )[:4]
    state_text = " ".join(
        name for name, value in strongest if float(value) >= 0.25
    )
    query = " ".join(part for part in (context_text, state_text) if part).strip()
    if not query:
        return []
    result = []
    try:
        bucket_matches = await bucket_mgr.search(
            query,
            limit=max_items,
            use_semantic=True,
            include_sealed=False,
            semantic_min_similarity=threshold,
            record_feedback=False,
        )
        for bucket in bucket_matches:
            metadata = bucket.get("metadata", {})
            result.append(
                {
                    "source": "memory",
                    "bucket_id": bucket.get("id", ""),
                    "name": str(metadata.get("name") or bucket.get("id", ""))[:80],
                    "excerpt": str(
                        bucket.get("matched_segment") or bucket.get("content", "")
                    ).strip()[:240],
                    "relevance": round(float(bucket.get("score", 0.0)) / 100.0, 4),
                    "semantic_similarity": bucket.get("semantic_score"),
                    "bm25_score": bucket.get("bm25_score"),
                }
            )
    except Exception as error:
        logger.warning("Bucket resonance search unavailable: %s", error)

    if mailbox_enabled:
        try:
            mailbox_matches = await search_mailbox(
                mailbox_store,
                bucket_mgr.embedding_index,
                query,
                limit=max_items,
                include_deleted=False,
            )
            for item in mailbox_matches:
                result.append(
                    {
                        "source": "mailbox",
                        "message_id": int(item["message_id"]),
                        "created_at": item.get("created_at"),
                        "excerpt": str(item.get("message", "")).strip()[:240],
                        "relevance": round(float(item.get("match_score", 0.0)), 4),
                        "semantic_similarity": item.get("semantic_score"),
                        "keyword_score": item.get("keyword_score"),
                    }
                )
        except Exception as error:
            logger.warning("Mailbox resonance search unavailable: %s", error)

    result.sort(key=lambda item: float(item.get("relevance", 0.0)), reverse=True)
    return result[:max_items]


async def _xinchao_task_context_provider(
    state: dict, event_contexts: list[dict]
) -> list[dict]:
    """Give darkflow a few unfinished matters without exposing them to Bark."""
    context_text = " ".join(
        str(item.get("context_card") or item.get("event_summary") or "")
        for item in event_contexts[-3:]
    ).strip()
    if context_text:
        return await task_service.context(context_text, limit=3)
    items = await task_service.store.list(status="open", limit=3)
    return [
        {
            "task_id": item["task_id"],
            "title": item["title"],
            "details": item["details"][:500],
            "importance": item["importance"],
        }
        for item in items
    ]


xinchao_service.set_memory_resonance_provider(
    _xinchao_memory_resonance_provider
)
xinchao_service.set_task_context_provider(_xinchao_task_context_provider)
behavior_service.set_feedback_callback(
    xinchao_service.apply_behavior_feedback
)


def _active_mcp_session_key() -> str:
    header_session = current_mcp_session_id()
    if header_session:
        return header_session
    try:
        context = mcp.get_context()
        session = context.session
    except (AttributeError, LookupError, RuntimeError, ValueError):
        return ""
    return f"runtime-session:{id(session)}"


def _active_mcp_event_key() -> str:
    middleware_key = current_mcp_event_id()
    if middleware_key:
        return middleware_key
    try:
        context = mcp.get_context()
        request_id = str(context.request_id)
    except (AttributeError, LookupError, RuntimeError, ValueError):
        return ""
    session_key = _active_mcp_session_key()
    return f"{session_key}\0{request_id}" if request_id else ""


def _write_sidecar_event_id(
    content: str,
    source_tool: str,
    source_ref: str = "",
) -> str:
    """Bind a request identity to the write it actually produced."""
    request_key = _active_mcp_event_key()
    if not request_key:
        return ""
    content_hash = hashlib.sha256(str(content).encode("utf-8")).hexdigest()[:24]
    return "\0".join(
        (request_key, str(source_tool), str(source_ref or ""), content_hash)
    )


async def _record_xinchao_event(
    content: str,
    source_tool: str,
    source_ref: str = "",
    external_event_id: str = "",
) -> dict:
    """Evaluate one successful narrative write without risking the memory write."""
    try:
        result = await xinchao_service.record_event(
            content,
            source_tool,
            source_ref,
            external_event_id=external_event_id
            or _write_sidecar_event_id(content, source_tool, source_ref),
        )
        if result.get("status") == "pending":
            logger.warning(
                "Xinchao event queued after %s write: %s",
                source_tool,
                result.get("error", "evaluation unavailable"),
            )
        elif result.get("status") == "applied":
            try:
                state = await xinchao_service.status()
                await behavior_service.schedule_event(result, state)
            except Exception as behavior_error:
                logger.warning(
                    "Behavior candidate scheduling failed after %s write: %s",
                    source_tool,
                    behavior_error,
                )
        return result
    except Exception as error:
        logger.exception("Xinchao hook failed after successful %s write: %s", source_tool, error)
        return {"status": "pending", "error": str(error)}


async def _record_write_sidecars(
    content: str,
    source_tool: str,
    source_ref: str = "",
    *,
    task_content: str = "",
) -> dict:
    """Update independent sidecars after a successful narrative write."""
    event_id = _write_sidecar_event_id(content, source_tool, source_ref)
    xinchao_result = await _record_xinchao_event(
        content,
        source_tool,
        source_ref,
        external_event_id=event_id,
    )
    try:
        task_result = await task_service.process_event(
            task_content or content,
            source_tool,
            source_ref,
            external_event_id=event_id,
        )
    except Exception as error:
        logger.exception(
            "Task extraction failed after successful %s write: %s",
            source_tool,
            error,
        )
        task_result = {"status": "error", "error": str(error)}
    try:
        fact_result = await fact_timeline_service.process_event(
            content,
            source_tool,
            source_ref,
            external_event_id=event_id,
        )
    except Exception as error:
        logger.exception(
            "Fact detection failed after successful %s write: %s",
            source_tool,
            error,
        )
        fact_result = {"status": "error", "error": str(error)}
    return {
        "tasks": task_result,
        "xinchao": xinchao_result,
        "fact_candidates": fact_result,
    }


async def _observe_mcp_activity(session_id: str, messages: list[dict]) -> None:
    relevant = [
        item
        for item in messages
        if item.get("method") in {"initialize", "tools/call"}
    ]
    if not relevant:
        return
    latest = relevant[-1]
    source = latest.get("tool_name") or latest.get("method") or "mcp"
    if source in {"heartbeat", "pulse_boot"}:
        return
    await xinchao_service.observe_presence(
        session_id=session_id,
        source=f"mcp:{source}",
        event_id=latest.get("request_id", ""),
    )

# --- Create MCP server instance / 创建 MCP 服务器实例 ---
# host="0.0.0.0" so Docker container's SSE is externally reachable
# stdio mode ignores host (no network)
mcp = FastMCP(
    "Ombre Brain",
    host="0.0.0.0",
    port=8000,
)


# =============================================================
# /health endpoint: lightweight keepalive
# 轻量保活接口
# For Cloudflare Tunnel or reverse proxy to ping, preventing idle timeout
# 供 Cloudflare Tunnel 或反代定期 ping，防止空闲超时断连
# =============================================================
@mcp.custom_route("/health", methods=["GET"])
async def health_check(request):
    from starlette.responses import JSONResponse
    try:
        stats = await bucket_mgr.get_stats()
        return JSONResponse({
            "status": "ok",
            "buckets": stats["permanent_count"] + stats["dynamic_count"],
            "decay_engine": (
                "running" if decay_engine.is_running else "idle_until_first_memory_operation"
            ),
        })
    except Exception as e:
        return JSONResponse({"status": "error", "detail": str(e)}, status_code=500)


# =============================================================
# Internal helper: append-or-create
# 内部辅助：只做原文追加或新建，绝不改写旧正文
# Shared by hold and grow to avoid duplicate logic
# hold 和 grow 共用，避免重复逻辑
# =============================================================
def _append_timestamp(now: datetime | None = None) -> str:
    return beijing_now(now).strftime("%Y-%m-%dT%H:%M")


def _append_source_text(
    old_content: str, new_content: str, *, timestamp: str | None = None
) -> str:
    """Append one timestamped segment while preserving every old character."""
    old_text = str(old_content or "")
    new_text = str(new_content or "")
    marker = f"--- {timestamp or _append_timestamp()} ---"
    separator = "\n" if old_text.endswith("\n") else "\n\n"
    return f"{old_text}{separator}{marker}\n{new_text}"


async def _append_or_create(
    content: str,
    tags: list,
    importance: int,
    domain: list,
    valence: float,
    arousal: float,
    name: str = "",
    allow_append: bool = True,
    ai_feeling: bool = False,
    trigger_date: str = "",
    write_result: dict | None = None,
) -> tuple[str, bool]:
    """
    Append to a semantically matching bucket, or create a new bucket.
    Existing source text is always an exact prefix of an append result.
    Returns (bucket_id_or_name, is_appended).
    """
    existing = []
    append_semantic_threshold = float(
        config.get("embeddings", {}).get("append_similarity_threshold", 0.62)
    )
    if allow_append:
        try:
            existing = await bucket_mgr.search(
                content,
                limit=10,
                use_semantic=True,
                semantic_min_similarity=append_semantic_threshold,
            )
        except Exception as e:
            logger.warning(f"Search for append failed, creating new / 追加搜索失败，新建: {e}")

    semantic_candidates = [
        bucket for bucket in existing
        if bucket.get("semantic_score", -1) >= append_semantic_threshold
        and bool(bucket.get("metadata", {}).get("ai_feeling", False))
        == bool(ai_feeling)
    ]
    if semantic_candidates:
        bucket = max(semantic_candidates, key=lambda item: item["semantic_score"])
        # --- Never append automatically into pinned/protected buckets ---
        # --- 不自动追加到钉选/保护桶 ---
        if not (bucket["metadata"].get("pinned") or bucket["metadata"].get("protected")):
            existing_domain = list(bucket["metadata"].get("domain", []))
            existing_tags = list(bucket["metadata"].get("tags", []))
            appended = _append_source_text(bucket["content"], content)
            updated = await bucket_mgr.update(
                bucket["id"],
                content=appended,
                # An appended fragment belongs to the existing bucket.  Its
                # one-off classification must not rename the whole bucket.
                tags=existing_tags or tags,
                importance=max(bucket["metadata"].get("importance", 5), importance),
                domain=existing_domain or domain,
                valence=valence,
                arousal=arousal,
                ai_feeling=ai_feeling,
                _history_operation="content_append",
                _require_content_prefix=True,
            )
            if not updated:
                raise RuntimeError(
                    f"追加写入被保护机制拒绝，旧正文未改动: {bucket['id']}"
                )
            if write_result is not None:
                write_result.update(
                    {
                        "bucket_id": bucket["id"],
                        "name": bucket["metadata"].get("name", bucket["id"]),
                        "domain": existing_domain or domain,
                    }
                )
            return bucket["metadata"].get("name", bucket["id"]), True

    bucket_id = await bucket_mgr.create(
        content=content,
        tags=tags,
        importance=importance,
        domain=domain,
        valence=valence,
        arousal=arousal,
        name=name or None,
        ai_feeling=ai_feeling,
        trigger_date=trigger_date,
        trigger_processed=False,
    )
    await _auto_link_new_bucket(bucket_id)
    await _auto_topic_new_bucket(bucket_id)
    if write_result is not None:
        write_result.update(
            {"bucket_id": bucket_id, "name": name or bucket_id, "domain": list(domain)}
        )
    return bucket_id, False


async def _check_conflicts(content: str) -> list[dict]:
    try:
        return await conflict_detector.detect(content)
    except Exception as e:
        logger.warning("Conflict detection failed without blocking memory write: %s", e)
        return []


def _format_conflict_warning(conflicts: list[dict]) -> str:
    if not conflicts:
        return ""
    lines = ["⚠️ 对账警告：新旧内容均已保留，请由对话中的人裁决。"]
    for conflict in conflicts:
        name = conflict.get("old_bucket_name") or conflict.get("old_bucket_id", "未知旧桶")
        bucket_id = conflict.get("old_bucket_id", "")
        lines.append(f"- 旧桶 {name} ({bucket_id})：{conflict.get('point', '事实不一致')}")
        if conflict.get("old_fact"):
            lines.append(f"  旧：{conflict['old_fact']}")
        if conflict.get("new_fact"):
            lines.append(f"  新：{conflict['new_fact']}")
    lines.append(
        "提示：这也可能是事实随时间发生了变化。确认后可用 timeline 工具记录新旧时间线；系统不会自动认定。"
    )
    return "\n" + "\n".join(lines)


async def _auto_link_new_bucket(bucket_id: str) -> list[tuple[str, float]]:
    """Link one newly created bucket to active, unsealed old buckets only."""
    if not relation_store.enabled:
        return []
    try:
        new_bucket = await bucket_mgr.get(bucket_id)
        if not new_bucket or new_bucket.get("metadata", {}).get("sealed", False):
            return []
        buckets = await bucket_mgr.list_all(
            include_archive=False, include_sealed=True
        )
        candidate_ids = [
            bucket["id"]
            for bucket in buckets
            if bucket["id"] != bucket_id
            and not bucket.get("metadata", {}).get("sealed", False)
            and bucket.get("metadata", {}).get("type") != "archived"
        ]
        candidate_id_set = set(candidate_ids)
        neighbors = await bucket_mgr.embedding_index.neighbors_for_bucket(
            bucket_id, candidate_ids
        )
        selected = [
            (other_id, similarity)
            for other_id, similarity in neighbors
            if other_id in candidate_id_set
            and similarity >= relation_store.similarity_threshold
        ][: relation_store.max_links]
        if selected:
            await relation_store.upsert_new_bucket_links(
                bucket_id,
                selected,
                bucket_mgr.embedding_index.model_name,
            )
        return selected
    except Exception as error:
        logger.warning(
            "Automatic relation linking failed without blocking bucket %s: %s",
            bucket_id,
            error,
        )
        return []


async def _visible_related_buckets(source_bucket: dict) -> list[dict]:
    """Resolve sidecar links while hiding sealed, archived, or missing buckets."""
    metadata = source_bucket.get("metadata", {})
    if metadata.get("sealed", False) or metadata.get("type") == "archived":
        return []
    max_links = max(1, int(getattr(relation_store, "max_links", 3)))
    visible = []

    # Confirmed fact history is more specific than semantic resemblance, so it
    # gets first claim on the small related-memory budget.
    try:
        fact_links = await fact_timeline_store.related_buckets(
            source_bucket["id"], limit=max_links * 3
        )
    except Exception as error:
        logger.warning("Fact-evolution relation lookup failed: %s", error)
        fact_links = []
    seen = set()
    for link in fact_links:
        related_id = str(link.get("bucket_id", ""))
        if not related_id or related_id in seen:
            continue
        try:
            bucket = await bucket_mgr.get(related_id)
        except Exception:
            continue
        if not bucket:
            continue
        related_meta = bucket.get("metadata", {})
        if related_meta.get("sealed", False) or related_meta.get("type") == "archived":
            continue
        visible.append(
            {
                "bucket_id": bucket["id"],
                "name": related_meta.get("name", bucket["id"]),
                "similarity": 1.0,
                "relation_type": "事实演化",
                "reason": (
                    f"同一事实：{link.get('fact_label', '')}；"
                    f"{link.get('effective_date', '')}"
                    f"{'起为当前版本' if link.get('is_current') else '前为历史版本'}"
                ),
            }
        )
        seen.add(related_id)
        if len(visible) >= max_links:
            break

    if len(visible) >= max_links or not relation_store.enabled:
        return visible
    try:
        related_rows = await relation_store.related_details(source_bucket["id"])
    except Exception as error:
        logger.warning("Related-memory lookup failed: %s", error)
        related_rows = []
    for relation in related_rows:
        related_id = relation["bucket_id"]
        if not related_id or related_id in seen:
            continue
        try:
            bucket = await bucket_mgr.get(related_id)
        except Exception as error:
            logger.warning("Related bucket %s could not be read: %s", related_id, error)
            continue
        if not bucket:
            continue
        related_meta = bucket.get("metadata", {})
        if related_meta.get("sealed", False) or related_meta.get("type") == "archived":
            continue
        visible.append(
            {
                "bucket_id": related_id,
                "name": related_meta.get("name", related_id),
                "similarity": float(relation["similarity"]),
                "relation_type": "语义相似",
                "reason": relation.get("reason", ""),
            }
        )
        seen.add(related_id)
        if len(visible) >= max_links:
            break
    return visible


def _render_related_buckets(related: list[dict]) -> str:
    if not related:
        return ""
    lines = ["【关联记忆】"]
    lines.extend(
        f"- [{item.get('relation_type', '语义相似')}] bucket_id: "
        f"{item['bucket_id']} | {item['name']}"
        + (
            f"（{item['reason']}）"
            if item.get("reason")
            else f"（相似度 {item['similarity']:.3f}）"
        )
        for item in related
    )
    return "\n" + "\n".join(lines)


async def _visible_fact_timeline_rows(rows: list[dict]) -> list[dict]:
    """Hide timeline rows backed by sealed, archived, or missing buckets."""
    visible = []
    bucket_cache: dict[str, dict | None] = {}
    for row in rows:
        source_type = str(row.get("source_type", "bucket") or "bucket").strip().lower()
        if source_type != "bucket":
            item = dict(row)
            item["source_bucket_name"] = "信箱" if source_type == "mailbox" else "人工确认"
            visible.append(item)
            continue
        source_id = str(row.get("source_bucket_id", "")).strip()
        if not source_id:
            continue
        if source_id not in bucket_cache:
            try:
                bucket_cache[source_id] = await bucket_mgr.get(source_id)
            except Exception as error:
                logger.warning("Timeline source %s could not be read: %s", source_id, error)
                bucket_cache[source_id] = None
        bucket = bucket_cache[source_id]
        if not bucket:
            continue
        metadata = bucket.get("metadata", {})
        if metadata.get("sealed", False) or metadata.get("type") == "archived":
            continue
        item = dict(row)
        item["source_bucket_name"] = metadata.get("name", source_id)
        visible.append(item)
    return visible


def _render_fact_timeline(rows: list[dict]) -> str:
    if not rows:
        return ""
    lines = ["【事实时间线】"]
    previous_key = None
    for row in rows:
        if row["fact_key"] != previous_key:
            lines.append(f"{row['fact_label']}：")
            previous_key = row["fact_key"]
        status = "现在" if bool(row.get("is_current")) else "以前"
        source_type = str(row.get("source_type", "bucket") or "bucket")
        source = (
            f"bucket_id: {row['source_bucket_id']}"
            if source_type == "bucket"
            else f"来源: {row.get('source_bucket_name', source_type)}"
        )
        lines.append(
            f"- {row['effective_date']}｜{row['fact_value']}（{status}）"
            f"｜{source}"
        )
    return "\n" + "\n".join(lines)


def _task_importance_label(value: int) -> str:
    return {1: "低", 2: "较低", 3: "普通", 4: "重要", 5: "紧要"}.get(
        int(value or 3), "普通"
    )


def _render_task_result(item: dict) -> str:
    details = " ".join(str(item.get("details", "")).split())
    detail_line = f"\n{details[:300]}" if details else ""
    score = item.get("match_score")
    score_line = f"\n[匹配度:{float(score):.3f}]" if score is not None else ""
    return (
        f"task_id: {item['task_id']}\n"
        f"[未竟·{_task_importance_label(item.get('importance', 3))}] "
        f"{item['title']}{score_line}{detail_line}"
    )


async def _timeline_for_bucket(bucket: dict) -> str:
    metadata = bucket.get("metadata", {})
    if metadata.get("sealed", False) or metadata.get("type") == "archived":
        return ""
    try:
        rows = await fact_timeline_store.versions_for_bucket(bucket["id"])
        return _render_fact_timeline(await _visible_fact_timeline_rows(rows))
    except Exception as error:
        logger.warning("Fact timeline lookup failed for %s: %s", bucket.get("id"), error)
        return ""


# =============================================================
# Tool 1: breath — Breathe
# 工具 1：breath — 呼吸
#
# No args: surface highest-weight unresolved memories (active push)
# 无参数：浮现权重最高的未解决记忆
# With args: search by keyword + emotion coordinates
# 有参数：按关键词+情感坐标检索记忆
# =============================================================
@mcp.tool()
async def breath(
    query: Optional[str] = None,
    max_results: int = 3,
    domain: str = "",
    valence: float = -1,
    arousal: float = -1,
    include_sealed: bool = False,
    feeling_only: bool = False,
    mood_resonance: bool = False,
) -> str:
    """breath retrieve search memory 检索/浮现记忆。mood_resonance=True 时按V/A心境距离排序"""
    if mood_resonance and not (
        0 <= valence <= 1 and 0 <= arousal <= 1
    ):
        return _with_response_seal(
            "心境共鸣需要同时提供 0~1 的 valence 和 arousal。"
        )

    await decay_engine.ensure_started()

    if mood_resonance and (not query or not query.strip()):
        try:
            candidates = await bucket_mgr.list_all(
                include_archive=False,
                include_sealed=include_sealed,
            )
        except Exception as error:
            logger.error("Mood resonance list failed: %s", error)
            return _with_response_seal("记忆系统暂时无法访问。")

        if domain:
            domain_filter = {item.strip().lower() for item in domain.split(",") if item.strip()}
            candidates = [
                bucket
                for bucket in candidates
                if {
                    str(item).lower()
                    for item in bucket.get("metadata", {}).get("domain", [])
                }
                & domain_filter
            ]
        if feeling_only:
            candidates = [
                bucket
                for bucket in candidates
                if bucket.get("metadata", {}).get("ai_feeling", False)
            ]

        matches = bucket_mgr.rank_by_mood(
            candidates,
            query_valence=valence,
            query_arousal=arousal,
            limit=max_results,
        )
        if not matches:
            return _with_response_seal("未找到带有效情绪坐标的相关记忆。")

        results = []
        for bucket in matches:
            try:
                summary = await _dehydrate_visible_segment(bucket)
                await bucket_mgr.touch(bucket["id"])
                timeline_text = await _timeline_for_bucket(bucket)
                results.append(
                    f"bucket_id: {bucket['id']}\n"
                    f"[心境距离: {bucket['mood_distance']:.3f}]\n{summary}"
                    f"{timeline_text}"
                )
            except Exception as error:
                logger.warning(
                    "Mood resonance result failed for %s: %s",
                    bucket.get("id", "?"),
                    error,
                )
        if not results:
            return _with_response_seal("未找到带有效情绪坐标的相关记忆。")
        return _with_response_seal("\n---\n".join(results))

    # --- No args or empty query: surfacing mode (weight pool active push) ---
    # --- 无参数或空query：浮现模式（权重池主动推送）---
    if not query or not query.strip():
        try:
            all_buckets = await bucket_mgr.list_all(include_archive=False)
        except Exception as e:
            logger.error(f"Failed to list buckets for surfacing / 浮现列桶失败: {e}")
            return _with_response_seal("记忆系统暂时无法访问。")

        if feeling_only:
            all_buckets = [
                bucket
                for bucket in all_buckets
                if bucket.get("metadata", {}).get("ai_feeling", False)
            ]

        # --- Pinned/protected buckets: always surface as core principles ---
        # --- 钉选桶：作为核心准则，始终浮现 ---
        pinned_buckets = [
            b for b in all_buckets
            if _pin_level(b["metadata"]) == "core"
        ]
        pinned_results = []
        for b in pinned_buckets:
            try:
                summary = await _dehydrate_visible_segment(b)
                timeline_text = await _timeline_for_bucket(b)
                pinned_results.append(
                    f"bucket_id: {b['id']}\n📌 [核心准则] {summary}"
                    f"{timeline_text}"
                )
            except Exception as e:
                logger.warning(f"Failed to dehydrate pinned bucket / 钉选桶脱水失败: {e}")
                continue

        # --- Unresolved buckets: surface top 2 by weight ---
        # --- 未解决桶：按权重浮现前 2 条 ---
        unresolved = [
            b for b in all_buckets
            if not b["metadata"].get("resolved", False)
            and b["metadata"].get("type") != "permanent"
            and not b["metadata"].get("pinned", False)
            and not b["metadata"].get("protected", False)
        ]

        scored = sorted(
            unresolved,
            key=lambda b: decay_engine.calculate_score(b["metadata"]),
            reverse=True,
        )
        top = scored[:2]
        dynamic_results = []
        for b in top:
            try:
                summary = await _dehydrate_visible_segment(b)
                await bucket_mgr.touch(b["id"])
                score = decay_engine.calculate_score(b["metadata"])
                timeline_text = await _timeline_for_bucket(b)
                dynamic_results.append(
                    f"bucket_id: {b['id']}\n[权重:{score:.2f}] {summary}"
                    f"{timeline_text}"
                )
            except Exception as e:
                logger.warning(f"Failed to dehydrate surfaced bucket / 浮现脱水失败: {e}")
                continue

        try:
            task_matches = await task_service.store.list(status="open", limit=2)
            task_results = [_render_task_result(item) for item in task_matches]
        except Exception as error:
            logger.warning("Task surfacing unavailable: %s", error)
            task_results = []

        if not pinned_results and not dynamic_results and not task_results:
            return _with_response_seal("权重池平静，没有需要处理的记忆。")

        parts = []
        if pinned_results:
            parts.append("=== 核心准则 ===\n" + "\n---\n".join(pinned_results))
        if dynamic_results:
            parts.append("=== 浮现记忆 ===\n" + "\n---\n".join(dynamic_results))
        if task_results:
            parts.append("=== 未竟 ===\n" + "\n---\n".join(task_results))
        return _with_response_seal("\n\n".join(parts))

    # --- With args: search mode / 有参数：检索模式 ---
    domain_filter = [d.strip() for d in domain.split(",") if d.strip()] or None
    q_valence = valence if 0 <= valence <= 1 else None
    q_arousal = arousal if 0 <= arousal <= 1 else None

    try:
        matches = await bucket_mgr.search(
            query,
            limit=max_results,
            domain_filter=domain_filter,
            query_valence=q_valence,
            query_arousal=q_arousal,
            include_sealed=include_sealed,
            feeling_only=feeling_only,
            mood_resonance=mood_resonance,
        )
    except Exception as e:
        logger.error(f"Search failed / 检索失败: {e}")
        return _with_response_seal("检索过程出错，请稍后重试。")

    results = []
    for bucket in matches:
        try:
            summary = await _dehydrate_visible_segment(bucket)
            await bucket_mgr.touch(bucket["id"])
            related = await _visible_related_buckets(bucket)
            timeline_text = await _timeline_for_bucket(bucket)
            distance_line = (
                f"[心境距离: {bucket['mood_distance']:.3f}]\n"
                if mood_resonance
                else ""
            )
            results.append(
                f"bucket_id: {bucket['id']}\n{distance_line}{summary}"
                f"{_render_related_buckets(related)}"
                f"{timeline_text}"
            )
        except Exception as e:
            logger.warning(f"Failed to dehydrate search result / 检索结果脱水失败: {e}")
            continue

    try:
        task_matches = await task_service.search(
            query,
            status="open",
            limit=max_results,
            include_closed=False,
        )
        task_results = [_render_task_result(item) for item in task_matches]
    except Exception as error:
        logger.warning("Task retrieval unavailable: %s", error)
        task_results = []

    if not results and not task_results:
        return _with_response_seal("未找到相关记忆。")

    retrieval_id = next(
        (
            str(bucket.get("retrieval_id", "")).strip()
            for bucket in matches
            if bucket.get("retrieval_id")
        ),
        "",
    )
    feedback_header = ""
    if retrieval_id:
        feedback_header = (
            "【检索编号提醒】\n"
            f"recall 时请带 retrieval_id={retrieval_id}，并同时带 bucket_id=<结果中的桶号>\n"
            "不要裸调 recall，否则系统无法记录本次真正采用的记忆。\n---\n"
        )
    rendered_parts = []
    if results:
        rendered_parts.append("\n---\n".join(results))
    if task_results:
        rendered_parts.append("【相关未竟】\n" + "\n---\n".join(task_results))
    return _with_response_seal(feedback_header + "\n\n".join(rendered_parts))


# =============================================================
# Tool 2: hold — Hold on to this
# 工具 2：hold — 握住，留下来
# =============================================================
@mcp.tool()
async def hold(
    content: str,
    tags: str = "",
    importance: int = 5,
    pinned: bool = False,
    feeling: bool = False,
    trigger_date: str = "",
) -> str:
    """hold auto store memory 未指定位置时自动归类存储。trigger_date 用 YYYY-MM-DD"""
    normalized_trigger = ""
    if trigger_date and trigger_date.strip():
        try:
            normalized_trigger = _normalize_trigger_date(trigger_date)
        except ValueError as error:
            return str(error)

    await decay_engine.ensure_started()

    # --- Input validation / 输入校验 ---
    if not content or not content.strip():
        return "内容为空，无法存储。"

    stored_content = _with_server_write_date(content)

    importance = max(1, min(10, importance))
    # --- Step 1: auto-tagging / 自动打标 ---
    try:
        analysis = await dehydrator.analyze(stored_content)
    except Exception as e:
        logger.warning(f"Auto-tagging failed, using defaults / 自动打标失败: {e}")
        analysis = {
            "domain": ["未分类"], "valence": 0.5, "arousal": 0.3,
            "tags": [], "suggested_name": "",
        }

    analysis = normalize_analysis(stored_content, analysis)
    domain = analysis["domain"]
    valence = analysis["valence"]
    arousal = analysis["arousal"]
    auto_tags = analysis["tags"]
    suggested_name = analysis.get("suggested_name", "")

    # The public `tags` parameter remains for old clients, but arbitrary labels
    # are deliberately ignored. Classification is owned by the server.
    all_tags = auto_tags
    conflicts = await _check_conflicts(stored_content)
    conflict_warning = _format_conflict_warning(conflicts)

    # --- Pinned buckets bypass automatic append and are created directly ---
    # --- 钉选桶跳过自动追加，直接新建到 permanent 目录 ---
    if pinned:
        bucket_id = await bucket_mgr.create(
            content=stored_content,
            tags=all_tags,
            importance=10,
            domain=domain,
            valence=valence,
            arousal=arousal,
            name=suggested_name or None,
            bucket_type="permanent",
            pinned=True,
            ai_feeling=feeling,
            trigger_date=normalized_trigger,
            trigger_processed=False,
        )
        await _auto_link_new_bucket(bucket_id)
        await _auto_topic_new_bucket(bucket_id)
        await _record_write_sidecars(stored_content, "hold", bucket_id)
        feeling_label = " [感受类]" if feeling else ""
        trigger_label = f" [触发:{normalized_trigger}]" if normalized_trigger else ""
        return (
            f"📌钉选→{bucket_id} {','.join(domain)}{feeling_label}"
            f"{trigger_label}{conflict_warning}"
        )

    # --- Step 2: append or create / 追加或新建 ---
    placement = {}
    result_name, is_appended = await _append_or_create(
        content=stored_content,
        tags=all_tags,
        importance=importance,
        domain=domain,
        valence=valence,
        arousal=arousal,
        name=suggested_name,
        allow_append=not conflicts and not normalized_trigger,
        ai_feeling=feeling,
        trigger_date=normalized_trigger,
        write_result=placement,
    )
    result_bucket_id = placement.get("bucket_id", result_name)
    result_domain = placement.get("domain", domain)
    await _record_write_sidecars(stored_content, "hold", result_bucket_id)

    action = "追加→" if is_appended else "新建→"
    feeling_label = " [感受类]" if feeling else ""
    trigger_label = f" [触发:{normalized_trigger}]" if normalized_trigger else ""
    return (
        f"{action}{result_name} [bucket_id:{result_bucket_id}] "
        f"{','.join(result_domain)}{feeling_label}"
        f"{trigger_label}{conflict_warning}"
    )


# =============================================================
# Tool 3: grow — Grow, fragments become memories
# 工具 3：grow — 生长，一天的碎片长成记忆
# =============================================================
@mcp.tool()
async def grow(content: str, message: str = "") -> str:
    """grow archive diary memory 自动归档到记忆桶;只写信箱请用 mailbox(message=...)"""
    await decay_engine.ensure_started()

    if not content or not content.strip():
        return "内容为空，无法整理。"

    # Global write invariant: one public write call may append to or create at
    # most one bucket. Only the explicit split_bucket tool may fan content out.
    stored_content = _with_server_write_date(content)
    try:
        analysis = await dehydrator.analyze(stored_content)
    except Exception as error:
        logger.warning("Grow classification failed, using defaults: %s", error)
        analysis = {
            "domain": ["未分类"],
            "valence": 0.5,
            "arousal": 0.3,
            "tags": [],
            "suggested_name": "",
        }

    analysis = normalize_analysis(stored_content, analysis)
    conflicts = await _check_conflicts(stored_content)
    conflict_warning = _format_conflict_warning(conflicts)
    placement = {}
    result_name, is_appended = await _append_or_create(
        content=stored_content,
        tags=analysis["tags"],
        importance=analysis.get("importance", 5)
        if isinstance(analysis.get("importance"), int)
        else 5,
        domain=analysis["domain"],
        valence=analysis["valence"],
        arousal=analysis["arousal"],
        name=analysis.get("suggested_name", ""),
        allow_append=not conflicts,
        write_result=placement,
    )

    action = "追加→" if is_appended else "新建→"
    result_bucket_id = placement.get("bucket_id", result_name)
    result_domain = placement.get("domain", analysis["domain"])
    response = (
        f"{action}{result_name} [bucket_id:{result_bucket_id}] "
        f"{','.join(result_domain)}"
        f"{conflict_warning}"
    )
    if message and message.strip():
        response += await _store_grow_message(message)
    # One grow call is one sidecar event even when it also leaves a message.
    combined_task_content = stored_content
    if message and message.strip():
        combined_task_content += f"\n\n信箱留言：\n{message.strip()}"
    await _record_write_sidecars(
        combined_task_content,
        "grow",
        result_bucket_id,
        task_content=combined_task_content,
    )
    return response


async def _store_grow_message(message: str) -> str:
    try:
        saved = await mailbox_store.add(message, source_tool="grow")
        return (
            f"\n📮留言已存入信箱 #{saved['message_id']}\n"
            f"时间: {saved['created_at']}"
        )
    except Exception as error:
        logger.error("Mailbox write failed after grow: %s", error)
        return "\n⚠️记忆已归档，但接力留言未能写入信箱。"


# =============================================================
# Tool 4: mailbox — Read window-to-window handoff messages
# 工具 4：mailbox — 查询窗口接力留言
# =============================================================
@mcp.tool()
async def mailbox(
    limit: int = 10,
    before_id: int = 0,
    message_id: int = 0,
    message: str = "",
    delete: bool = False,
    confirm: bool = False,
    history: bool = False,
    include_deleted: bool = False,
    query: str = "",
) -> str:
    """mailbox search read write edit delete 搜索/读取/写入/修改信箱留言"""

    def format_message(item: dict) -> str:
        lines = [
            f"message_id: {item['message_id']}",
            f"时间: {item['created_at']}",
            f"来源: {item['source_tool']}",
        ]
        if item.get("updated_at"):
            lines.append(f"最后修改: {item['updated_at']}")
        if item.get("deleted_at"):
            lines.append(f"删除时间: {item['deleted_at']}")
        lines.append(item["message"])
        return "\n".join(lines)

    text = message.strip()
    if history and (delete or text):
        return _with_response_seal("历史查询不能同时修改或删除留言。")
    if delete and text:
        return _with_response_seal("修改和删除不能在同一次调用中进行。")
    if (history or delete) and message_id <= 0:
        return _with_response_seal("删除或查询历史时必须提供 message_id。")

    try:
        if text and message_id <= 0:
            saved = await mailbox_store.add(text, source_tool="mailbox")
            await _record_write_sidecars(
                text,
                "mailbox",
                str(saved["message_id"]),
            )
            return _with_response_seal(
                f"留言已单独存入信箱 #{saved['message_id']}。\n"
                f"时间: {saved['created_at']}\n"
                "未创建或修改任何记忆桶。"
            )

        if history:
            snapshots = await mailbox_store.history(message_id, limit=limit)
            if not snapshots:
                return _with_response_seal(
                    f"留言 #{message_id} 暂无修改或删除历史。"
                )
            parts = [
                f"=== 留言 #{message_id} 历史（{len(snapshots)}）==="
            ]
            for snapshot in snapshots:
                operation = (
                    "修改前快照"
                    if snapshot["operation"] == "update"
                    else "删除前快照"
                )
                parts.append(
                    f"history_id: {snapshot['history_id']}\n"
                    f"快照时间: {snapshot['snapshot_at']}\n"
                    f"操作: {operation}\n"
                    f"原写入时间: {snapshot['created_at']}\n"
                    f"来源: {snapshot['source_tool']}\n"
                    f"{snapshot['message']}"
                )
            return _with_response_seal("\n---\n".join(parts))

        if delete:
            current = await mailbox_store.get(message_id)
            if not current:
                return _with_response_seal(
                    f"找不到可删除的留言 #{message_id}。"
                )
            if not confirm:
                return _with_response_seal(
                    "【信箱删除演习】\n"
                    "尚未删除。确认无误后用相同参数并设置 confirm=True。\n"
                    f"{format_message(current)}"
                )
            deleted = await mailbox_store.delete(message_id)
            return _with_response_seal(
                f"留言 #{message_id} 已删除，删除前原文已保存到历史快照。\n"
                f"删除时间: {deleted['deleted_at']}"
            )

        if text:
            current = await mailbox_store.get(message_id)
            if not current:
                return _with_response_seal(
                    f"找不到可修改的留言 #{message_id}。"
                )
            if not confirm:
                return _with_response_seal(
                    "【信箱修改演习】\n"
                    "尚未修改。确认无误后用相同参数并设置 confirm=True。\n"
                    f"原文:\n{current['message']}\n---\n拟修改为:\n{text}"
                )
            updated = await mailbox_store.update(message_id, text)
            return _with_response_seal(
                f"留言 #{message_id} 已修改，修改前原文已保存到历史快照。\n"
                f"最后修改: {updated['updated_at']}\n{updated['message']}"
            )

        if confirm:
            return _with_response_seal(
                "confirm=True 只能与明确的修改内容或 delete=True 一起使用。"
            )

        if message_id > 0:
            item = await mailbox_store.get(
                message_id, include_deleted=include_deleted
            )
            if not item:
                return _with_response_seal(f"找不到留言 #{message_id}。")
            return _with_response_seal(format_message(item))

        if query.strip():
            messages = await search_mailbox(
                mailbox_store,
                bucket_mgr.embedding_index,
                query,
                limit=limit,
                include_deleted=include_deleted,
            )
        elif include_deleted:
            messages = await mailbox_store.list(
                limit=limit,
                before_id=before_id,
                include_deleted=True,
            )
        else:
            messages = await mailbox_store.list(
                limit=limit, before_id=before_id
            )
    except Exception as error:
        return _with_response_seal(f"信箱操作失败: {error}")

    if not messages:
        return _with_response_seal("信箱暂无留言。")
    heading = "信箱搜索结果" if query.strip() else "信箱留言"
    parts = [f"=== {heading}（{len(messages)}）==="]
    parts.extend(format_message(item) for item in messages)
    if query.strip():
        return _with_response_seal("\n---\n".join(parts))
    continuation = f"mailbox(before_id={messages[-1]['message_id']}"
    if include_deleted:
        continuation += ", include_deleted=True"
    continuation += ")"
    parts.append(f"继续向前查询: {continuation}")
    return _with_response_seal("\n---\n".join(parts))


# =============================================================
# Tool: tasks — Independent unfinished-matter ledger
# 工具：tasks — 独立未竟账本
# =============================================================
@mcp.tool()
async def tasks(
    action: str = "list",
    task_id: int = 0,
    query: str = "",
    title: str = "",
    details: str = "",
    importance: int = -1,
    status: str = "",
    limit: int = 20,
    confirm: bool = False,
) -> str:
    """tasks todo unfinished manage 搜索、新增、修改、完成或取消未竟事项;重要度1-5"""

    def render(item: dict) -> str:
        lines = [
            f"task_id: {item['task_id']}",
            f"状态: {item['status']}",
            f"重要程度: {item['importance']}（{_task_importance_label(item['importance'])}）",
            f"事项: {item['title']}",
        ]
        if item.get("details"):
            lines.append(f"详情: {item['details']}")
        lines.append(f"更新时间: {item['updated_at']}")
        sources = item.get("sources") or []
        if sources:
            source = sources[0]
            lines.append(
                f"最近来源: {source.get('source_type', '')} {source.get('source_ref', '')}".rstrip()
            )
        return "\n".join(lines)

    normalized = str(action or "list").strip().lower()
    aliases = {
        "add": "create", "新增": "create", "搜索": "search", "查询": "list",
        "修改": "update", "完成": "complete", "取消": "cancel",
        "重开": "reopen", "删除": "delete", "历史": "history",
    }
    normalized = aliases.get(normalized, normalized)
    limit = max(1, min(100, int(limit)))
    try:
        if normalized == "create":
            item = await task_service.create_manual(
                title, details, importance if 1 <= importance <= 5 else 3,
                source="mcp:tasks"
            )
            hormone_content = f"我新增了一件未完成的事：{item['title']}。"
            await xinchao_service.record_event(
                hormone_content,
                "tasks",
                str(item["task_id"]),
                external_event_id=_write_sidecar_event_id(
                    hormone_content, "tasks", str(item["task_id"])
                ),
            )
            return _with_response_seal("未竟事项已新增。\n" + render(item))

        if normalized in {"update", "complete", "cancel", "reopen"}:
            if task_id <= 0:
                return _with_response_seal("请提供 task_id。")
            changes = {}
            if normalized == "update":
                if title.strip():
                    changes["title"] = title
                if details.strip():
                    changes["details"] = details
                if 1 <= importance <= 5:
                    changes["importance"] = importance
                if status.strip():
                    changes["status"] = status
                if not changes:
                    return _with_response_seal("没有提供需要修改的字段。")
            else:
                changes["status"] = {
                    "complete": "completed", "cancel": "cancelled", "reopen": "open"
                }[normalized]
            item = await task_service.update_manual(task_id, **changes)
            state_words = {
                "open": "重新成为待处理事项",
                "completed": "已经完成",
                "cancelled": "已经取消",
            }
            hormone_content = (
                f"未竟事项“{item['title']}”{state_words[item['status']]}。"
            )
            await xinchao_service.record_event(
                hormone_content,
                "tasks",
                str(item["task_id"]),
                external_event_id=_write_sidecar_event_id(
                    hormone_content, "tasks", str(item["task_id"])
                ),
            )
            return _with_response_seal("未竟事项已更新。\n" + render(item))

        if normalized == "delete":
            if task_id <= 0:
                return _with_response_seal("请提供 task_id。")
            item = await task_service.store.get(task_id)
            if not item:
                return _with_response_seal(f"找不到未竟事项 #{task_id}。")
            if not confirm:
                return _with_response_seal(
                    "【未竟删除演习】\n尚未删除。确认后使用相同参数并设置 confirm=True。\n"
                    + render(item)
                )
            deleted = await task_service.store.delete(task_id)
            return _with_response_seal(
                f"未竟事项 #{task_id} 已删除。\n事项: {deleted['title']}"
            )

        if normalized == "history":
            if task_id <= 0:
                return _with_response_seal("请提供 task_id。")
            rows = await task_service.store.history(task_id, limit)
            if not rows:
                return _with_response_seal(f"未竟事项 #{task_id} 暂无历史版本。")
            return _with_response_seal(
                "\n---\n".join(
                    f"history_id: {row['history_id']}\n快照时间: {row['snapshot_at']}\n"
                    f"操作: {row['operation']}\n状态: {row['status']}\n"
                    f"重要程度: {row['importance']}\n事项: {row['title']}\n{row['details']}"
                    for row in rows
                )
            )

        if normalized not in {"list", "search"}:
            return _with_response_seal(
                "action 支持 list、search、create、update、complete、cancel、reopen、history、delete。"
            )
        if task_id > 0:
            item = await task_service.store.get(task_id)
            return _with_response_seal(render(item)) if item else _with_response_seal(
                f"找不到未竟事项 #{task_id}。"
            )
        if query.strip() or normalized == "search":
            rows = await task_service.search(
                query, status=status, limit=limit, include_closed=not bool(status)
            )
        else:
            rows = await task_service.store.list(status=status, limit=limit)
        if not rows:
            return _with_response_seal("没有符合条件的未竟事项。")
        return _with_response_seal("\n---\n".join(render(item) for item in rows))
    except Exception as error:
        return _with_response_seal(f"未竟操作失败: {error}")


# =============================================================
# Tool 5: trace — Trace, redraw the outline of a memory
# 工具 5：trace — 描摹，重新勾勒记忆的轮廓
# Also handles deletion (delete=True)
# 同时承接删除功能
# =============================================================
@mcp.tool()
async def trace(
    bucket_id: str,
    name: str = "",
    domain: str = "",
    valence: float = -1,
    arousal: float = -1,
    importance: int = -1,
    tags: str = "",
    resolved: int = -1,
    pinned: int = -1,
    delete: bool = False,
    content: str = "",
    append: bool = False,
    history: bool = False,
    history_limit: int = 10,
    sealed: int = -1,
    feeling: int = -1,
    trigger_date: Optional[str] = None,
    trigger_processed: int = -1,
    pin_level: str = "",
    confirm_pin_level: bool = False,
    sort_order: Optional[int] = None,
) -> str:
    """trace exact bucket modify append 指定桶修改或追加。钉选分级须 confirm_pin_level=True"""

    if history:
        if not bucket_id or not bucket_id.strip():
            return "请提供有效的 bucket_id。"
        snapshots = await bucket_mgr.get_history(bucket_id, history_limit)
        if not snapshots:
            return f"记忆桶 {bucket_id} 暂无历史快照。"
        parts = [f"=== 记忆桶 {bucket_id} 历史版本（{len(snapshots)}） ==="]
        for snapshot in snapshots:
            metadata_text = json.dumps(
                snapshot["metadata"], ensure_ascii=False, sort_keys=True
            )
            parts.append(
                f"[快照 #{snapshot['snapshot_id']}] {snapshot['snapshot_at']}\n"
                f"操作类型: {snapshot['operation_type']}\n"
                f"完整元数据: {metadata_text}\n完整内容:\n{snapshot['content']}"
            )
        return "\n---\n".join(parts)

    if not bucket_id or not bucket_id.strip():
        return "请提供有效的 bucket_id。"

    # --- Delete mode / 删除模式 ---
    if delete:
        success = await bucket_mgr.delete(bucket_id)
        if success:
            try:
                await dehydrator.summary_cache.delete(bucket_id)
            except Exception as error:
                logger.warning("Summary cache cleanup failed for %s: %s", bucket_id, error)
        return f"已遗忘记忆桶: {bucket_id}" if success else f"未找到记忆桶: {bucket_id}"

    bucket = await bucket_mgr.get(bucket_id)
    if not bucket:
        return f"未找到记忆桶: {bucket_id}"

    # --- Collect only fields actually passed / 只收集用户实际传入的字段 ---
    updates = {}
    if content:
        old_content = str(bucket.get("content", ""))
        if append:
            updates["content"] = _append_source_text(old_content, content)
        else:
            if len(content) < len(old_content):
                return (
                    "拒绝写入：新正文短于旧正文，旧正文未改动。"
                    "如需删除内容，请先人工确认并使用专门的拆分/恢复流程。"
                )
            updates["content"] = content
    if name:
        updates["name"] = name
    requested_categories = ",".join(
        value for value in (domain, tags) if value.strip()
    )
    if requested_categories:
        try:
            category = parse_category(requested_categories)
        except ValueError as error:
            return str(error)
        updates["domain"] = [category]
        updates["tags"] = [category]
    if 0 <= valence <= 1:
        updates["valence"] = valence
    if 0 <= arousal <= 1:
        updates["arousal"] = arousal
    if 1 <= importance <= 10:
        updates["importance"] = importance
    if resolved in (0, 1):
        updates["resolved"] = bool(resolved)
    effective_sealed = bool(bucket.get("metadata", {}).get("sealed", False))
    if sealed in (0, 1):
        effective_sealed = bool(sealed)
        updates["sealed"] = effective_sealed
        if effective_sealed:
            updates["pinned"] = False
    if pinned == 1 and effective_sealed:
        return "封存桶不能设置为钉选；请先解除封存。"
    if pinned in (0, 1):
        updates["pinned"] = bool(pinned)
        if pinned == 1:
            updates["importance"] = 10  # pinned → lock importance
    requested_pin_level = str(pin_level or "").strip().lower()
    if requested_pin_level:
        if requested_pin_level not in ("core", "important"):
            return "pin_level 只能是 core（核心）或 important（重要）。"
        if pinned == 0:
            return "不能在取消钉选的同时设置钉选级别。"
        if effective_sealed:
            return "封存桶不能设置钉选级别；请先解除封存。"
        if bucket.get("metadata", {}).get("protected", False) and requested_pin_level != "core":
            return "保护桶固定属于核心级，不能降为重要级。"
        if not confirm_pin_level:
            label = "核心钉选" if requested_pin_level == "core" else "重要钉选"
            return (
                "【钉选分级演习】\n"
                "尚未修改。请人工确认后，用相同参数并设置 confirm_pin_level=True。\n"
                f"bucket_id: {bucket_id}\n"
                f"当前级别: {_pin_level(bucket.get('metadata', {})) or '普通记忆'}\n"
                f"计划级别: {label}"
            )
        updates["pinned"] = True
        updates["pin_level"] = requested_pin_level
        updates["importance"] = 10
    if feeling in (0, 1):
        updates["ai_feeling"] = bool(feeling)
    effective_trigger = str(
        bucket.get("metadata", {}).get("trigger_date", "")
    ).strip()
    if trigger_date is not None:
        requested_trigger = trigger_date.strip()
        if requested_trigger:
            try:
                effective_trigger = _normalize_trigger_date(requested_trigger)
            except ValueError as error:
                return str(error)
            updates["trigger_date"] = effective_trigger
            updates["trigger_processed"] = False
        else:
            effective_trigger = ""
            updates["trigger_date"] = ""
    if trigger_processed in (0, 1):
        if not effective_trigger:
            return "没有触发日期，无法设置处理状态。"
        updates["trigger_processed"] = bool(trigger_processed)
    if sort_order is not None:
        updates["sort_order"] = int(sort_order)

    if not updates:
        return "没有任何字段需要修改。"

    history_operation = "metadata_update"
    if "content" in updates:
        history_operation = "content_append" if append else "content_replace"
    success = await bucket_mgr.update(
        bucket_id,
        _history_operation=history_operation,
        _require_content_prefix=append and "content" in updates,
        **updates,
    )
    if not success:
        return f"修改失败: {bucket_id}"

    if append and content and "content" in updates:
        await _record_xinchao_event(
            content,
            "trace_append",
            bucket_id,
        )

    changed_parts = []
    for key, value in updates.items():
        if key == "content":
            label = "content=\u5df2\u8ffd\u52a0" if append else "content=\u5df2\u66ff\u6362"
            changed_parts.append(label)
        elif key == "ai_feeling":
            changed_parts.append(f"feeling={value}")
        elif key == "trigger_date":
            changed_parts.append(
                f"trigger_date={value}" if value else "trigger_date=已清除"
            )
        elif key == "trigger_processed":
            changed_parts.append(f"trigger_processed={value}")
        elif key == "pin_level":
            changed_parts.append(
                "pin_level=核心钉选" if value == "core" else "pin_level=重要钉选"
            )
        else:
            changed_parts.append(f"{key}={value}")
    changed = ", ".join(changed_parts)
    # Explicit hint about resolved state change semantics
    # 特别提示 resolved 状态变化的语义
    if "resolved" in updates:
        if updates["resolved"]:
            changed += " → 已沉底，只在关键词触发时重新浮现"
        else:
            changed += " → 已重新激活，将参与浮现排序"
    return f"已修改记忆桶 {bucket_id}: {changed}"


# =============================================================
# Tool 6: pulse_boot — Compact startup context
# 工具 6：pulse_boot — 开机专用上下文
# =============================================================
@mcp.tool()
async def pulse_boot() -> str:
    """pulse_boot startup summary 开机摘要:钉选、近期归档、待办及预留区"""
    await history_retention_engine.ensure_started()
    settings = config.get("pulse_boot", {})
    session_id = _active_mcp_session_key()
    max_chars = max(2000, min(12000, int(settings.get("max_chars", 6000))))
    core_lead_chars = max(40, min(160, int(settings.get("core_lead_chars", 80))))
    core_max_items = max(1, min(30, int(settings.get("core_max_items", 20))))
    first_bucket_id = str(settings.get("first_bucket_id", "")).strip()
    feeling_write_reminder_enabled = bool(
        settings.get("feeling_write_reminder", True)
    )
    thought_limit = max(1, min(8, int(settings.get("thought_limit", 3))))
    mailbox_context = await _pulse_boot_mailbox_context()
    darkflow = None
    xinchao_state = {}
    try:
        xinchao_state = await xinchao_service.consume_boot(
            mailbox_context=mailbox_context
        )
        xinchao_text = (
            xinchao_service.render_compact(xinchao_state)
            if xinchao_state.get("available")
            else ""
        )
        # A darkflow belongs to exactly one absence cycle. Falling back to a
        # previously-read slot can revive an old handoff and hide a newer mail.
        darkflow = xinchao_state.get("darkflow_item")
    except Exception as error:
        logger.warning("pulse_boot Xinchao handoff failed: %s", error)
        xinchao_text = ""
    try:
        active_thoughts = await xinchao_service.list_private_thoughts(
            status="active", limit=thought_limit
        )
        thought_lines = []
        for item in active_thoughts:
            text = str(item.get("thought_text") or "").strip()
            if not text:
                continue
            kind = "执念" if item.get("status") == "obsession" else "闪念"
            thought_lines.append(f"- [{kind}] {text}")
        thought_text = "\n".join(thought_lines)
    except Exception as error:
        logger.warning("pulse_boot private thought read failed: %s", error)
        thought_text = ""
    try:
        treasury_summary = await treasury_store.summary()
        treasury_text = ""
        if int(treasury_summary.get("entry_count", 0)) > 0:
            treasury_text = (
                f"余额 {treasury_summary['symbol']}{treasury_summary['balance']}｜"
                f"累计收入 {treasury_summary['symbol']}{treasury_summary['total_income']}｜"
                f"累计支出 {treasury_summary['symbol']}{treasury_summary['total_expense']}"
            )
    except Exception as error:
        logger.warning("pulse_boot treasury read failed: %s", error)
        treasury_text = ""

    task_completion_ids = []
    try:
        task_snapshot = await task_service.boot_snapshot(
            open_limit=max(1, min(20, int(settings.get("task_open_limit", 10)))),
            completed_limit=max(1, min(10, int(settings.get("task_completed_limit", 5)))),
        )
        open_tasks = task_snapshot["open"]
        completed_tasks = task_snapshot["completed"]
        task_completion_ids = [int(item["task_id"]) for item in completed_tasks]
        task_lines = [
            f"- #{item['task_id']} [{_task_importance_label(item['importance'])}] {item['title']}"
            + (f"｜{' '.join(item['details'].split())[:160]}" if item.get("details") else "")
            for item in open_tasks
        ]
        completed_lines = [
            f"- #{item['task_id']} {item['title']}｜完成时间 {item.get('completed_at') or item['updated_at']}"
            for item in completed_tasks
        ]
        task_text = ""
        if task_lines:
            task_text += "还没有完成或仍要处理：\n" + "\n".join(task_lines)
        if completed_lines:
            if task_text:
                task_text += "\n"
            task_text += "刚刚完成（仅本次告知）：\n" + "\n".join(completed_lines)
    except Exception as error:
        logger.warning("pulse_boot task read failed: %s", error)
        task_text = ""
        task_completion_ids = []

    mailbox_text = await _pulse_boot_mailbox_section(mailbox_context)
    behavior_handoff_ids = []
    try:
        pending_behaviors = await behavior_service.store.list_pending_handoff(limit=30)
        hidden_silence_ids = []
        cycle_behaviors = []
        for item in pending_behaviors:
            context = item.get("context") or {}
            phase = str(context.get("phase") or "")
            legacy_silence = (
                not phase
                and int(context.get("event_count", 0)) == 0
                and int(item.get("stage_index", 0))
                in {1, behavior_service.SILENCE_NUDGE_STAGE}
            )
            if phase == "silence" or legacy_silence:
                hidden_silence_ids.append(int(item["action_id"]))
                continue
            cycle_behaviors.append(item)
            if len(cycle_behaviors) >= 10:
                break
        if hidden_silence_ids:
            await behavior_service.store.purge_handoff(hidden_silence_ids)
        behavior_handoff_ids = [int(item["action_id"]) for item in cycle_behaviors]
        behavior_text = "\n".join(
            f"- {item.get('delivered_at') or item['decided_at']}｜"
            f"已通过 Bark 发送：{item['content']}"
            + (
                f"｜用户已于 {item['acknowledged_at']} 点过“我看到了”"
                if item.get("acknowledged_at")
                else "｜等待用户回应"
            )
            for item in cycle_behaviors
        )
    except Exception as error:
        logger.warning("pulse_boot behavior log read failed: %s", error)
        behavior_text = ""

    topic_directory_text = "\n".join(
        f"- {main_topic}：{'、'.join(subtopics)}"
        for main_topic, subtopics in TOPIC_TREE.items()
    )

    try:
        buckets = await bucket_mgr.list_all(include_archive=True)
    except Exception as error:
        return _with_response_seal(f"开机记忆暂时无法访问: {error}")

    fixed = sorted(
        [
            bucket
            for bucket in buckets
            if _pin_level(bucket.get("metadata", {})) == "core"
            and not bucket.get("metadata", {}).get("sealed", False)
        ],
        key=lambda bucket: (
            bucket.get("id") == first_bucket_id,
            _pulse_boot_sort_order(bucket),
            _pulse_boot_time_key(bucket),
        ),
        reverse=True,
    )[:core_max_items]

    fixed_parts = []
    for index, bucket in enumerate(fixed, start=1):
        metadata = bucket.get("metadata", {})
        lead = _pulse_boot_core_lead(bucket, core_lead_chars)
        fixed_parts.append(
            f"{index}. {metadata.get('name') or bucket['id']}｜bucket_id: {bucket['id']}"
            + (f"\n   开头: {lead}" if lead else "")
        )
    fixed_text = "\n".join(fixed_parts)

    feeling_write_reminder = ""
    if feeling_write_reminder_enabled:
        feeling_write_reminder = (
            "【感受写入提醒】\n"
            "写叙事记忆或信箱时，用第一人称写清事件、当时想法、情绪或身体反应；"
            "平静就写平静，不编造。值得长期保存的感受先交用户确认，再调用 "
            'hold(content="我当时……", feeling=true)。'
        )
    handoff_parts = []
    has_darkflow = bool(darkflow and str(darkflow.get("content", "")).strip())
    if has_darkflow:
        boot_elapsed_seconds = int(
            xinchao_state.get("elapsed_seconds", darkflow.get("elapsed_seconds", 0))
        )
        handoff_parts.append(
            "【有一条新的暗涌】\n"
            "静默期间形成了一条新的内在沉淀，正文不在开机摘要中展开；"
            "需要时再调用 xinchao_status 或 inner_state 深读。\n"
            "从上一轮明确结束到本次开机已过去："
            f"{xinchao_service.format_elapsed_zh(boot_elapsed_seconds)}"
        )
    elif bool(xinchao_state.get("static_ready", False)):
        boot_elapsed_seconds = int(xinchao_state.get("elapsed_seconds", 0))
        handoff_parts.append(
            "【静默时长】\n"
            "从上一轮明确结束到本次开机已过去："
            f"{xinchao_service.format_elapsed_zh(boot_elapsed_seconds)}"
        )
    if mailbox_text:
        handoff_parts.append(f"【信箱最新留言】\n{mailbox_text}")
    handoff_text = "\n\n".join(handoff_parts)

    sections = [
        "=== Clio 开机记忆 ===",
        (
            "【可用能力】\n"
            "找记忆 breath｜读原文 recall｜写记忆 hold/grow｜修改 trace｜"
            "目录 cabinet｜信箱 mailbox｜未竟 tasks｜状态 xinchao_status/heartbeat｜内在 inner_state｜"
            "时间线 timeline｜检索反馈 feedback｜拆桶 split_bucket｜小金库 treasury。\n"
            "按当前对话需要调用，不要为了检查而把全库读一遍。"
        ),
    ]
    if fixed_text:
        sections.append(
            "【固定层：核心记忆目录】\n"
            f"{fixed_text}"
        )
    if topic_directory_text:
        sections.append(
            "【全库主题导航】\n"
            "只显示方向，不读取正文；按需调用 cabinet 进入主题。\n"
            f"{topic_directory_text}"
        )
    xinchao_text = _pulse_boot_hormone_summary(xinchao_text)
    if xinchao_text:
        sections.append(
            "【激素：离开期间的状态】\n"
            f"{xinchao_text}"
        )
    if thought_text:
        sections.append(f"【心念】\n{thought_text}")
    if handoff_text:
        sections.append(handoff_text)
    if behavior_text:
        sections.append(
            "【本轮静默期间实际行为】\n"
            f"{behavior_text}"
        )
    if task_text:
        sections.append(f"【未竟】\n{task_text}")
    if treasury_text:
        sections.append(f"【AI小金库】\n{treasury_text}")
    if feeling_write_reminder:
        sections.append(feeling_write_reminder)
    body = "\n\n".join(sections)
    if len(body) > max_chars:
        suffix = "\n\n【开机资料已达到固定上限，其余记忆请按需使用 recall 深读。】"
        body = body[: max(1, max_chars - len(suffix))].rstrip() + suffix
    if darkflow and "【有一条新的暗涌】" in body:
        try:
            await xinchao_service.mark_darkflow_delivered(
                int(darkflow["cycle_id"])
            )
        except Exception as error:
            logger.warning("pulse_boot darkflow delivery mark failed: %s", error)
    if behavior_handoff_ids and behavior_text in body:
        try:
            await behavior_service.store.purge_handoff(behavior_handoff_ids)
        except Exception as error:
            logger.warning("pulse_boot behavior handoff purge failed: %s", error)
    if task_completion_ids and "刚刚完成（仅本次告知）" in body:
        try:
            await task_service.store.mark_completions_delivered(task_completion_ids)
        except Exception as error:
            logger.warning("pulse_boot task completion handoff failed: %s", error)
    try:
        await xinchao_service.observe_presence(
            session_id=session_id,
            source="mcp:pulse_boot",
            event_id=_active_mcp_event_key(),
            start_cycle=False,
        )
    except Exception as error:
        logger.warning("pulse_boot presence timer could not start: %s", error)
    return _with_response_seal(body)


# =============================================================
# Tool 7: xinchao_status — read-only emotional state preview
# 工具 7：xinchao_status — 只读预览，不结束当前心潮周期
# =============================================================
@mcp.tool()
async def xinchao_status() -> str:
    """xinchao_status hormone emotion mood state 只读查看沉默期间激素状态;不会清零或结束周期"""
    try:
        state = await xinchao_service.status()
        return _with_response_seal(xinchao_service.render_full(state))
    except Exception as error:
        logger.warning("Xinchao status read failed: %s", error)
        return _with_response_seal("激素状态暂时无法读取，记忆桶未受影响。")


@mcp.tool()
async def inner_state() -> str:
    """inner_state thoughts resonance tension inspect 查看当前心念、记忆共振与内在张力;只读不清空"""
    try:
        state = await xinchao_service.status()
        thoughts = await xinchao_service.list_private_thoughts(
            status="active", limit=12
        )
        darkflow = await xinchao_service.darkflow_status()
    except Exception as error:
        logger.warning("Inner state read failed: %s", error)
        return _with_response_seal("内在状态暂时无法读取。")

    sections = ["=== 内在状态（只读） ==="]
    thought_lines = []
    for item in thoughts:
        text = str(item.get("thought_text") or "").strip()
        if not text:
            continue
        kind = "执念" if item.get("status") == "obsession" else "闪念"
        strength = float(item.get("current_strength", item.get("intensity", 0.0)))
        count = max(1, int(item.get("occurrence_count", 1)))
        line = f"- [{kind}｜强度 {strength:.2f}｜出现 {count} 次] {text}"
        reason = str(item.get("reason") or "").strip()
        if reason:
            line += f"\n  触发缘由: {reason}"
        if item.get("last_seen"):
            line += f"\n  最近出现: {item['last_seen']}"
        thought_lines.append(line)
    if thought_lines:
        sections.append("【心念】\n" + "\n".join(thought_lines))

    resonance_lines = []
    for item in (darkflow or {}).get("memory_resonance", [])[:6]:
        score = float(item.get("similarity", item.get("relevance", 0.0)) or 0.0)
        excerpt = " ".join(str(item.get("excerpt") or "").split())
        if not excerpt:
            continue
        if item.get("source") == "mailbox":
            source = f"信箱 #{item.get('message_id', '')}"
        else:
            source = str(item.get("name") or item.get("bucket_id") or "记忆")
        resonance_lines.append(f"- [{source}｜{score:.2f}] {excerpt}")
    if resonance_lines:
        sections.append("【记忆共振】\n" + "\n".join(resonance_lines))

    pipes = state.get("pipes") or {}
    if pipes:
        resting = {"满足", "自省"}
        outward = sorted(
            ((name, float(value)) for name, value in pipes.items() if name not in resting),
            key=lambda item: item[1],
            reverse=True,
        )[:3]
        restraint_weights = {"满足": 1.0, "自省": 0.85, "难过": 0.45, "生气": 0.35}
        restraints = sorted(
            (
                (name, float(pipes.get(name, 0.0)) * weight)
                for name, weight in restraint_weights.items()
            ),
            key=lambda item: item[1],
            reverse=True,
        )[:2]
        strongest = outward[0] if outward else ("平静", 0.0)
        counterweight = restraints[0] if restraints else ("无", 0.0)
        tension_lines = [
            "向外: " + "｜".join(f"{name} {value:.2f}" for name, value in outward),
            "收束: " + "｜".join(f"{name} {value:.2f}" for name, value in restraints),
            f"张力差: {strongest[1] - counterweight[1]:+.2f}",
        ]
        sections.append("【张力】\n" + "\n".join(line for line in tension_lines if line))

    sections.append("仅供理解当前状态；本次读取不会清空、消耗或修改任何内容。")
    return _with_response_seal("\n\n".join(sections))


@mcp.tool()
async def heartbeat(event_id: str = "") -> str:
    """heartbeat presence alive 无正文报到，不开启静默或暗涌计时"""
    result = await xinchao_service.observe_presence(
        session_id=_active_mcp_session_key(),
        source="mcp:heartbeat",
        event_id=event_id or _active_mcp_event_key(),
        start_cycle=False,
    )
    return _with_response_seal(
        "已记录仍在当前窗口，未读取或写入任何记忆正文。\n"
        f"时间（UTC+8）：{beijing_now().isoformat(timespec='seconds')}\n"
        "状态：仅记录当前窗口仍在，不影响激素、心念、静默或暗涌。"
    )


# =============================================================
# Tool 8: pulse — Heartbeat, system status + memory listing
# 工具 8：pulse — 脉搏，系统状态 + 记忆列表
# =============================================================
@mcp.tool()
async def pulse(
    include_archive: bool = False,
    page: int = 1,
    page_size: int = 0,
    content_id: str = "",
) -> str:
    """pulse list status buckets 系统状态和桶列表;长结果建议 page_size=2500"""
    next_call = (
        f"pulse(include_archive={str(include_archive).lower()}, "
        f"page={{page}}, page_size={page_size}, content_id=\"{{content_id}}\")"
    )
    snapshot_scope = f"pulse:{include_archive}:{page_size}"
    if content_id:
        return _paginate_response(
            "", page, page_size, next_call, content_id, snapshot_scope
        )

    try:
        stats = await bucket_mgr.get_stats()
    except Exception as e:
        return _with_response_seal(f"获取系统状态失败: {e}")

    status = (
        f"=== Ombre Brain 记忆系统 ===\n"
        f"固化记忆桶: {stats['permanent_count']} 个\n"
        f"动态记忆桶: {stats['dynamic_count']} 个\n"
        f"归档记忆桶: {stats['archive_count']} 个\n"
        f"总存储大小: {stats['total_size_kb']:.1f} KB\n"
        f"衰减引擎: {'运行中' if decay_engine.is_running else '等待首次记忆操作'}\n"
    )

    # --- List all bucket summaries / 列出所有桶摘要 ---
    try:
        buckets = await bucket_mgr.list_all(include_archive=include_archive)
    except Exception as e:
        return _with_response_seal(status + f"\n列出记忆桶失败: {e}")

    if not buckets:
        return _with_response_seal(status + "\n记忆库为空。")

    lines = []
    for b in buckets:
        meta = b.get("metadata", {})
        pin_level = _pin_level(meta)
        if pin_level == "core":
            icon = "📌"
            pin_tag = " [核心钉选]"
        elif pin_level == "important":
            icon = "📍"
            pin_tag = " [重要钉选]"
        elif meta.get("type") == "permanent":
            icon = "📦"
            pin_tag = ""
        elif meta.get("type") == "archived":
            icon = "🗄️"
            pin_tag = ""
        elif meta.get("resolved", False):
            icon = "✅"
            pin_tag = ""
        else:
            icon = "💭"
            pin_tag = ""
        try:
            score = decay_engine.calculate_score(meta)
        except Exception:
            score = 0.0
        domains = ",".join(meta.get("domain", []))
        val = meta.get("valence", 0.5)
        aro = meta.get("arousal", 0.3)
        resolved_tag = " [已解决]" if meta.get("resolved", False) else ""
        lines.append(
            f"bucket_id: {b['id']} | "
            f"{icon} [{meta.get('name', b['id'])}]{pin_tag}{resolved_tag} "
            f"主题:{domains} "
            f"情感:V{val:.1f}/A{aro:.1f} "
            f"重要:{meta.get('importance', '?')} "
            f"权重:{score:.2f} "
            f"标签:{','.join(meta.get('tags', []))}"
        )

    body = status + "\n=== 记忆列表 ===\n" + "\n".join(lines)
    return _paginate_response(
        body, page, page_size, next_call, content_id, snapshot_scope
    )


# =============================================================
# Tool 8: digest_preview — read-only automatic digestion rehearsal
# 工具 8：digest_preview — 自动消化只读演习
# =============================================================
@mcp.tool()
async def digest_preview() -> str:
    """digest_preview dry-run report 生成自动消化演习报告;不修改任何桶"""
    try:
        report = await digestion_planner.preview()
    except Exception as error:
        logger.error("Digestion preview tool failed: %s", error)
        return _with_response_seal("自动消化演习暂时无法生成。")
    return _with_response_seal(digestion_planner.render(report))


# =============================================================
# Tool: calendar - read everything recorded on one Beijing date
# 工具：calendar - 按北京时间查看某一天留下的记录
# =============================================================
@mcp.tool()
async def calendar(
    date: str = "",
    include_archived: bool = False,
    include_sealed: bool = False,
) -> str:
    """calendar date memory day 按北京时间查看某一天写入的记忆、信箱、心念及其他记录"""
    target = str(date or "").strip() or beijing_now().date().isoformat()
    try:
        datetime.strptime(target, "%Y-%m-%d")
    except ValueError:
        return _with_response_seal("日期必须使用有效的 YYYY-MM-DD，例如 2026-08-11。")

    try:
        (
            buckets,
            mailbox_items,
            behavior_items,
            task_items,
            treasury_items,
            thoughts,
            darkflow,
            facts,
        ) = await asyncio.gather(
            bucket_mgr.list_all(
                include_archive=include_archived,
                include_sealed=include_sealed,
            ),
            mailbox_store.search_pool(include_deleted=False, limit=5000),
            behavior_service.store.list(limit=500),
            task_service.store.list(limit=500),
            treasury_store.list(limit=500, include_deleted=False),
            xinchao_service.list_private_thoughts(status="all", limit=500),
            xinchao_service.darkflow_status(),
            fact_timeline_store.list_facts(limit=200),
        )
        day = build_calendar_day(
            target,
            buckets=buckets,
            mailbox=mailbox_items,
            behaviors=behavior_items,
            tasks=task_items,
            treasury=treasury_items,
            thoughts=thoughts,
            darkflow=darkflow,
            facts=facts,
            include_archived=include_archived,
            include_sealed=include_sealed,
        )
    except Exception as error:
        logger.error("Calendar read failed for %s: %s", target, error)
        return _with_response_seal("记忆日历暂时无法读取，任何原始记录都没有被修改。")
    return _with_response_seal(format_calendar_day(day))


# =============================================================
# Tool 9: timeline — dated versions of changing facts
# 工具 9：timeline — 事实的新旧时间线
# =============================================================
@mcp.tool()
async def timeline(
    fact: str,
    value: str = "",
    effective_date: str = "",
    source_bucket_id: str = "",
    confirm: bool = False,
) -> str:
    """timeline facts date 查询事实时间线;提供 value/date/source 后须 confirm=True 记录"""
    try:
        fact_key, fact_label = fact_timeline_store.normalize_fact_key(fact)
    except ValueError as error:
        return _with_response_seal(str(error))

    # Read mode never writes. An exact fact name keeps unrelated timelines apart.
    if not value and not effective_date and not source_bucket_id:
        try:
            rows = await fact_timeline_store.versions(fact_label)
            visible = await _visible_fact_timeline_rows(rows)
        except Exception as error:
            logger.error("Fact timeline read failed: %s", error)
            return _with_response_seal("事实时间线暂时无法访问。")
        if not visible:
            return _with_response_seal("未找到可见的事实时间线。")
        return _with_response_seal(_render_fact_timeline(visible).lstrip())

    try:
        fact_value = fact_timeline_store.normalize_value(value)
        effective = fact_timeline_store.normalize_effective_date(effective_date)
    except ValueError as error:
        return _with_response_seal(str(error))
    source_id = str(source_bucket_id or "").strip()
    if not source_id:
        return _with_response_seal("必须提供来源 bucket_id。")

    try:
        source_bucket = await bucket_mgr.get(source_id)
    except Exception as error:
        logger.warning("Timeline source validation failed for %s: %s", source_id, error)
        source_bucket = None
    source_metadata = source_bucket.get("metadata", {}) if source_bucket else {}
    if (
        not source_bucket
        or source_metadata.get("sealed", False)
        or source_metadata.get("type") == "archived"
    ):
        return _with_response_seal("来源桶不可用或不允许加入事实时间线。")

    if not confirm:
        preview = (
            "【事实时间线演习】\n"
            "尚未写入。请人工核对无误后，用相同参数并设置 confirm=True。\n"
            f"事实: {fact_label}\n"
            f"生效日期: {effective}\n"
            f"内容: {fact_value}\n"
            f"来源 bucket_id: {source_id}"
        )
        return _with_response_seal(preview)

    try:
        saved = await fact_timeline_store.record(
            fact=fact_label,
            value=fact_value,
            effective_date=effective,
            source_bucket_id=source_id,
        )
        rows = await fact_timeline_store.versions(fact_label)
        visible = await _visible_fact_timeline_rows(rows)
    except ValueError as error:
        return _with_response_seal(str(error))
    except Exception as error:
        logger.error("Fact timeline write failed: %s", error)
        return _with_response_seal("事实时间线写入失败，原记忆桶未受影响。")

    status = "记录没有变化" if saved["status"] == "unchanged" else "已记录"
    body = f"{status}：{fact_label}\n{_render_fact_timeline(visible).lstrip()}"
    return _with_response_seal(body)


# =============================================================
# Tool 10: recall — exact source text for one known bucket
# 工具 10：recall — 按桶 ID 读取完整原文
# =============================================================
@mcp.tool()
async def recall(
    bucket_id: str,
    include_sealed: bool = False,
    page: int = 1,
    page_size: int = 0,
    content_id: str = "",
    retrieval_id: str = "",
    segments_per_page: int = 1,
    newest_first: bool = True,
    limit: int = 1,
    before_id: str = "",
) -> str:
    """recall read bucket content 默认读取最新一包;before_id 向前翻历史;整桶可用 page_size=2500"""
    source_id = str(bucket_id or "").strip()
    if not source_id:
        return _with_response_seal("请提供有效的 bucket_id。")
    character_next_call = (
        f"recall(bucket_id={json.dumps(source_id, ensure_ascii=False)}, "
        f"include_sealed={str(include_sealed).lower()}, "
        f"page={{page}}, page_size={page_size}, content_id=\"{{content_id}}\")"
    )
    character_scope = f"recall:chars:{source_id}:{include_sealed}:{page_size}"
    segment_scope = (
        f"recall:segments:{source_id}:{include_sealed}:"
        f"{segments_per_page}:{newest_first}"
    )
    if content_id and page_size > 0:
        return _paginate_response(
            "", page, page_size, character_next_call, content_id, character_scope
        )
    if content_id and page_size == 0:
        return _paginate_recall_segments(
            {},
            page,
            segments_per_page,
            newest_first,
            content_id,
            segment_scope,
        )

    try:
        bucket = await bucket_mgr.get(source_id)
    except Exception as error:
        logger.warning("Recall failed for %s: %s", source_id, error)
        bucket = None
    metadata = bucket.get("metadata", {}) if bucket else {}
    if not bucket or (metadata.get("sealed", False) and not include_sealed):
        return _with_response_seal("未找到可读取的记忆桶。")

    feedback_note = ""
    if retrieval_id and page == 1:
        try:
            feedback_result = await retrieval_feedback_store.record(
                retrieval_id,
                source_id,
                rating=1,
                source="recall",
            )
            feedback_status = feedback_result.get("status", "")
            if feedback_status in {"recorded", "updated", "unchanged"}:
                feedback_note = "检索反馈: 已记录为本次查询采用。\n"
            elif feedback_status == "expired":
                feedback_note = "检索反馈: 检索编号已过期，原文读取不受影响。\n"
            elif feedback_status == "not_in_results":
                feedback_note = "检索反馈: 该桶不属于对应的检索结果，未记录。\n"
            elif feedback_status == "disabled":
                feedback_note = "检索反馈: 当前未启用，原文读取不受影响。\n"
        except Exception as error:
            logger.warning("Recall feedback failed without blocking read: %s", error)
            feedback_note = "检索反馈: 暂时无法记录，原文读取不受影响。\n"

    name = metadata.get("name", source_id)
    bucket_type = metadata.get("type", "unknown")
    if page_size == 0:
        payload = {
            "bucket_id": source_id,
            "name": name,
            "bucket_type": bucket_type,
            "include_sealed": include_sealed,
            "content": bucket.get("content", ""),
            "created_at": metadata.get("created", ""),
        }
        if before_id or (page == 1 and newest_first and segments_per_page == 1):
            return _recall_segment_cursor(
                payload,
                limit=limit,
                before_id=before_id,
                feedback_note=feedback_note,
            )
        return _paginate_recall_segments(
            payload,
            page,
            segments_per_page,
            newest_first,
            content_id,
            segment_scope,
            feedback_note,
        )

    body = (
        f"bucket_id: {source_id}\n"
        f"名称: {name}\n"
        f"类型: {bucket_type}\n"
        f"{feedback_note}"
        f"完整原文:\n{bucket.get('content', '')}"
    )
    return _paginate_response(
        body,
        page,
        page_size,
        character_next_call,
        content_id,
        character_scope,
    )


# =============================================================
# Tool 11: split_bucket — copy selected source into a new child bucket
# 工具 11：split_bucket — 按时间或标记复制原文到新子桶
# =============================================================
@mcp.tool()
async def split_bucket(
    bucket_id: str,
    start_time: str = "",
    end_time: str = "",
    start_marker: str = "",
    end_marker: str = "",
    new_name: str = "",
    include_sealed: bool = False,
    confirm: bool = False,
) -> str:
    """split_bucket copy time range markers 拆分长桶;只新建子桶,源桶原文不变"""
    source_id = str(bucket_id or "").strip()
    if not source_id:
        return _with_response_seal("请提供有效的 bucket_id。")

    bucket = await bucket_mgr.get(source_id)
    metadata = bucket.get("metadata", {}) if bucket else {}
    if not bucket or (metadata.get("sealed", False) and not include_sealed):
        return _with_response_seal("未找到可拆分的记忆桶。")

    use_time = bool(start_time.strip() or end_time.strip())
    use_marker = bool(start_marker or end_marker)
    if use_time == use_marker:
        return _with_response_seal(
            "请二选一：提供 start_time/end_time，或提供 start_marker/end_marker。"
        )

    source = str(bucket.get("content", ""))
    selection = ""
    selection_label = ""
    if use_time:
        normalized_start = start_time.strip()
        normalized_end = end_time.strip()
        for label, value in (("start_time", normalized_start), ("end_time", normalized_end)):
            if not value:
                continue
            try:
                datetime.strptime(value, "%Y-%m-%dT%H:%M")
            except ValueError:
                return _with_response_seal(f"{label} 必须使用 YYYY-MM-DDTHH:MM。")
        if normalized_start and normalized_end and normalized_start > normalized_end:
            return _with_response_seal("start_time 不能晚于 end_time。")
        selected = [
            segment
            for segment in _split_timestamped_segments(source)
            if segment["timestamp"]
            and (not normalized_start or segment["timestamp"] >= normalized_start)
            and (not normalized_end or segment["timestamp"] <= normalized_end)
        ]
        if not selected:
            return _with_response_seal("指定时间范围内没有可拆分的时间段。")
        selection = "".join(segment["text"] for segment in selected)
        selection_label = (
            f"时间范围 {normalized_start or '最早'} 至 {normalized_end or '最新'}，"
            f"共 {len(selected)} 段"
        )
    else:
        if not start_marker:
            return _with_response_seal("按标记拆分时必须提供 start_marker。")
        start_index = source.find(start_marker)
        if start_index < 0:
            return _with_response_seal("没有找到 start_marker，源桶未改动。")
        if end_marker:
            end_index = source.find(end_marker, start_index + len(start_marker))
            if end_index < 0:
                return _with_response_seal("没有找到 end_marker，源桶未改动。")
            end_label = repr(end_marker)
        else:
            end_index = len(source)
            end_label = "原文末尾"
        selection = source[start_index:end_index]
        selection_label = f"从标记 {start_marker!r} 到 {end_label}"

    source_hash = hashlib.sha256(source.encode("utf-8")).hexdigest()
    child_name = new_name.strip() or f"{metadata.get('name', source_id)}-拆分"
    if not confirm:
        return _with_response_seal(
            "【拆分演习】\n"
            "尚未新建子桶，源桶不会被修改。\n"
            f"源 bucket_id: {source_id}\n"
            f"源正文 SHA-256: {source_hash}\n"
            f"选择范围: {selection_label}\n"
            f"将复制字数: {len(selection)}\n"
            f"新桶名称: {child_name}\n"
            "确认后请使用相同参数并设置 confirm=True。"
        )

    child_id = await bucket_mgr.create(
        content=selection,
        tags=list(metadata.get("tags", [])),
        importance=int(metadata.get("importance", 5)),
        domain=list(metadata.get("domain", ["未分类"])),
        valence=float(metadata.get("valence", 0.5)),
        arousal=float(metadata.get("arousal", 0.3)),
        name=child_name,
        ai_feeling=bool(metadata.get("ai_feeling", False)),
    )
    source_after = await bucket_mgr.get(source_id)
    after_content = str(source_after.get("content", "")) if source_after else ""
    after_hash = hashlib.sha256(after_content.encode("utf-8")).hexdigest()
    if after_content != source or after_hash != source_hash:
        logger.critical("Source changed during split operation: %s", source_id)
        return _with_response_seal(
            f"严重错误：拆分期间源桶校验不一致。新子桶: {child_id}。请停止写入并检查。"
        )
    await _auto_link_new_bucket(child_id)
    await _auto_topic_new_bucket(child_id)
    return _with_response_seal(
        "拆分完成：只新建了子桶，源桶原文未改动。\n"
        f"源 bucket_id: {source_id}\n"
        f"源正文 SHA-256: {source_hash}\n"
        f"新 bucket_id: {child_id}\n"
        f"复制字数: {len(selection)}"
    )


# =============================================================
# Tool: cabinet — browse the topic hierarchy without loading bucket bodies
# 工具：cabinet — 按主题目录逐层浏览，不读取正文
# =============================================================
@mcp.tool()
async def cabinet(
    main_topic: str = "",
    subtopic: str = "",
    offset: int = 0,
    limit: int = 20,
) -> str:
    """cabinet topics directory browse memory 按主目录和子目录逐层定位记忆桶，不读取正文"""
    main = str(main_topic or "").strip()
    sub = str(subtopic or "").strip()
    if not main:
        lines = ["【主题主目录】", "先选择一个主目录，再调用 cabinet(main_topic=\"目录名\")。"]
        lines.extend(f"- {name}" for name in TOPIC_TREE)
        return _with_response_seal("\n".join(lines))
    if main not in TOPIC_TREE:
        return _with_response_seal(
            "主目录不存在。可用目录：" + "、".join(TOPIC_TREE)
        )
    if not sub:
        lines = [
            f"【{main}｜子目录】",
            "选择一个子目录，再同时提供 main_topic 和 subtopic。",
        ]
        lines.extend(f"- {name}" for name in TOPIC_TREE[main])
        return _with_response_seal("\n".join(lines))
    try:
        validate_topic(main, sub)
    except ValueError as error:
        return _with_response_seal(str(error))

    assignments = await topic_store.list(main, sub)
    visible = []
    for assignment in assignments:
        bucket = await bucket_mgr.get(assignment["bucket_id"])
        if not bucket:
            continue
        metadata = bucket.get("metadata", {})
        if metadata.get("sealed") or str(metadata.get("type", "")).lower() == "archived":
            continue
        visible.append(bucket)
    safe_offset = max(0, int(offset))
    safe_limit = max(1, min(50, int(limit)))
    page = visible[safe_offset : safe_offset + safe_limit]
    lines = [
        f"【{main} → {sub}】",
        "这里只列目录项；确定需要后再用 recall(bucket_id=\"...\", limit=1) 深读。",
    ]
    for bucket in page:
        metadata = bucket.get("metadata", {})
        lines.append(
            f"- {metadata.get('name') or bucket['id']}｜bucket_id: {bucket['id']}｜"
            f"最近: {metadata.get('last_active') or metadata.get('created') or '未知'}"
        )
    if not page:
        lines.append("这个子目录暂时没有已归类的可见记忆。")
    elif safe_offset + len(page) < len(visible):
        lines.append(
            f"还有更多目录项：cabinet(main_topic={main!r}, subtopic={sub!r}, "
            f"offset={safe_offset + len(page)}, limit={safe_limit})"
        )
    return _with_response_seal("\n".join(lines))


# =============================================================
# Tool 12: feedback — explicit retrieval-result feedback
# 工具 12：feedback — 明确评价一次检索结果
# =============================================================
@mcp.tool()
async def feedback(
    retrieval_id: str,
    bucket_id: str,
    rating: str,
    confirm: bool = False,
) -> str:
    """feedback rate breath result 评价一次 breath 结果;rating 仅支持 useful 或 irrelevant"""
    normalized = str(rating or "").strip().lower()
    if normalized not in {"useful", "irrelevant"}:
        return _with_response_seal("rating 只能是 useful 或 irrelevant。")
    if normalized == "irrelevant" and not confirm:
        return _with_response_seal(
            "尚未记录负反馈。确认该结果确实与本次查询无关后，"
            "请使用相同参数并设置 confirm=True。"
        )

    try:
        result = await retrieval_feedback_store.record(
            retrieval_id,
            bucket_id,
            rating=1 if normalized == "useful" else -1,
            source="explicit",
        )
    except Exception as error:
        logger.warning("Explicit retrieval feedback failed: %s", error)
        return _with_response_seal("检索反馈暂时无法记录，记忆桶未受影响。")

    status = result.get("status", "")
    if status == "disabled":
        message = "检索反馈当前未启用。"
    elif status == "expired":
        message = "检索编号已过期，请重新执行 breath 后再反馈。"
    elif status == "not_in_results":
        message = "该桶不属于这次 breath 的返回结果，未记录反馈。"
    elif status == "unchanged":
        message = "这条反馈已经记录过，没有重复累计。"
    elif normalized == "useful":
        message = "已记录为有用；只会小幅调整相似查询的排序。"
    else:
        message = "已记录为不相关；只会小幅调整相似查询的排序。"
    return _with_response_seal(message)


# =============================================================
# Tool 13: treasury — AI-owned income and expense ledger
# 工具 13：treasury — AI 自己的小金库
# =============================================================
@mcp.tool()
async def treasury(
    action: str = "status",
    amount: float = 0,
    reason: str = "",
    entry_type: str = "",
    entry_id: int = 0,
    occurred_at: str = "",
    limit: int = 10,
    before_id: int = 0,
    confirm: bool = False,
    include_deleted: bool = False,
) -> str:
    """treasury wallet income expense balance AI小金库;原因自由填写,系统自动计算总金额和累计收支"""

    def format_entry(item: dict) -> str:
        kind = "收入" if item["entry_type"] == "income" else "支出"
        sign = "+" if item["entry_type"] == "income" else "-"
        deleted = f"\n删除时间: {item['deleted_at']}" if item.get("deleted_at") else ""
        updated = f"\n最后修改: {item['updated_at']}" if item.get("updated_at") else ""
        return (
            f"entry_id: {item['entry_id']} | {kind} "
            f"{sign}{treasury_store.symbol}{item['amount']}\n"
            f"时间: {item['occurred_at']}\n"
            f"原因: {item['reason']}\n"
            f"来源: {item['source']}{updated}{deleted}"
        )

    normalized_action = str(action or "status").strip().lower()
    aliases = {
        "收入": "income",
        "支出": "expense",
        "余额": "status",
        "查询": "list",
        "明细": "list",
        "修改": "update",
        "删除": "delete",
        "历史": "history",
    }
    normalized_action = aliases.get(normalized_action, normalized_action)
    valid_actions = {
        "status",
        "income",
        "expense",
        "list",
        "update",
        "delete",
        "history",
    }
    if normalized_action not in valid_actions:
        return _with_response_seal(
            "action 仅支持 status、income、expense、list、update、delete、history。"
        )

    try:
        if normalized_action == "status":
            summary = await treasury_store.summary()
            latest = await treasury_store.list(limit=min(max(1, limit), 5))
            body = "=== AI小金库 ===\n" + _format_treasury_summary(summary)
            if latest:
                body += "\n\n最近账目:\n" + "\n---\n".join(
                    format_entry(item) for item in latest
                )
            else:
                body += "\n\n还没有账目。"
            return _with_response_seal(body)

        if normalized_action in {"income", "expense"}:
            result = await treasury_store.record(
                normalized_action,
                amount,
                reason,
                occurred_at or None,
                source="mcp:treasury",
            )
            kind = "收入" if normalized_action == "income" else "支出"
            return _with_response_seal(
                f"{kind}已记入小金库。\n"
                f"{format_entry(result['entry'])}\n\n"
                f"{_format_treasury_summary(result['summary'])}"
            )

        if normalized_action == "list":
            entries = await treasury_store.list(
                limit=limit,
                before_id=before_id,
                entry_type=entry_type,
                include_deleted=include_deleted,
            )
            summary = await treasury_store.summary()
            if not entries:
                return _with_response_seal(
                    "没有符合条件的账目。\n" + _format_treasury_summary(summary)
                )
            body = (
                f"=== AI小金库明细（{len(entries)}笔）===\n"
                + _format_treasury_summary(summary)
                + "\n\n"
                + "\n---\n".join(format_entry(item) for item in entries)
                + f"\n\n继续向前查询: treasury(action=\"list\", before_id={entries[-1]['entry_id']})"
            )
            return _with_response_seal(body)

        if entry_id <= 0:
            return _with_response_seal(
                "修改、删除或查询历史时必须提供有效的 entry_id。"
            )

        if normalized_action == "history":
            snapshots = await treasury_store.history(entry_id, limit=limit)
            if not snapshots:
                return _with_response_seal(f"账目 #{entry_id} 暂无修改或删除历史。")
            parts = [f"=== 账目 #{entry_id} 历史（{len(snapshots)}）==="]
            for snapshot in snapshots:
                operation = "修改前快照" if snapshot["operation"] == "update" else "删除前快照"
                parts.append(
                    f"history_id: {snapshot['history_id']} | {operation}\n"
                    f"快照时间: {snapshot['snapshot_at']}\n"
                    f"{format_entry(snapshot)}"
                )
            return _with_response_seal("\n---\n".join(parts))

        current = await treasury_store.get(entry_id)
        if not current:
            return _with_response_seal(f"找不到可操作的账目 #{entry_id}。")

        if normalized_action == "delete":
            if not confirm:
                return _with_response_seal(
                    "【小金库删除演习】\n尚未删除。确认后用相同参数并设置 confirm=True。\n"
                    + format_entry(current)
                )
            result = await treasury_store.delete(entry_id)
            return _with_response_seal(
                f"账目 #{entry_id} 已删除，删除前完整记录已保存。\n"
                + _format_treasury_summary(result["summary"])
            )

        proposed_type = entry_type.strip() or current["entry_type"]
        proposed_amount = amount if amount > 0 else current["amount"]
        proposed_reason = reason if reason.strip() else current["reason"]
        proposed_time = occurred_at.strip() or current["occurred_at"]
        if not confirm:
            kind = "收入" if proposed_type in {"income", "收入"} else "支出"
            return _with_response_seal(
                "【小金库修改演习】\n尚未修改。确认后用相同参数并设置 confirm=True。\n"
                f"原记录:\n{format_entry(current)}\n---\n"
                f"拟修改为: {kind} {treasury_store.symbol}{proposed_amount}\n"
                f"时间: {proposed_time}\n原因: {proposed_reason}"
            )
        result = await treasury_store.update(
            entry_id,
            entry_type=proposed_type,
            amount=proposed_amount,
            reason=proposed_reason,
            occurred_at=proposed_time,
        )
        return _with_response_seal(
            f"账目 #{entry_id} 已修改，修改前完整记录已保存。\n"
            f"{format_entry(result['entry'])}\n\n"
            f"{_format_treasury_summary(result['summary'])}"
        )
    except Exception as error:
        logger.warning("Treasury operation failed: %s", error)
        return _with_response_seal(f"小金库操作失败: {error}")


# --- Entry point / 启动入口 ---
async def _xinchao_settlement_loop() -> None:
    interval = max(
        30,
        min(600, int(config.get("xinchao", {}).get("settle_interval_seconds", 60))),
    )
    await asyncio.sleep(min(15, interval))
    while True:
        try:
            mailbox_context = await _pulse_boot_mailbox_context()
            state = await xinchao_service.status()
            settled = await xinchao_service.settle_darkflow(
                mailbox_context=mailbox_context
            )
            darkflow = await xinchao_service.pending_darkflow()
            state = await xinchao_service.status()
            if state.get("interaction_phase") != "absence":
                darkflow = None
            due_results = await behavior_service.process_due(
                state, mailbox_context, darkflow
            )
            already_sent = any(
                item.get("status") in {"sent", "rehearsal"}
                for item in due_results
            )
            if not already_sent:
                await behavior_service.process(darkflow, state, mailbox_context)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            logger.warning("Xinchao background settlement failed: %s", error)
        await asyncio.sleep(interval)


async def _task_retention_loop() -> None:
    """Permanently remove delivered completed tasks after their grace period."""
    interval = max(3600, int(task_service.retention_check_hours * 3600))
    while True:
        try:
            result = await task_service.purge_expired_completed()
            if result.get("deleted"):
                logger.info(
                    "Permanently purged %s delivered completed tasks",
                    result["deleted"],
                )
        except asyncio.CancelledError:
            raise
        except Exception as error:
            logger.warning("Completed task retention failed: %s", error)
        await asyncio.sleep(interval)


if __name__ == "__main__":
    _validate_response_seal()
    transport = config.get("transport", "stdio")
    logger.info(f"Ombre Brain starting | transport: {transport}")

    if transport in ("sse", "streamable-http"):
        import threading
        import uvicorn
        from starlette.middleware.cors import CORSMiddleware

        # --- Application-level keepalive: ping /health every 60s ---
        # --- 应用层保活：每 60 秒 ping 一次 /health，防止 Cloudflare Tunnel 空闲断连 ---
        async def _keepalive_loop():
            await asyncio.sleep(10)  # Wait for server to fully start
            async with httpx.AsyncClient() as client:
                while True:
                    try:
                        await client.get("http://localhost:8000/health", timeout=5)
                        logger.debug("Keepalive ping OK / 保活 ping 成功")
                    except Exception as e:
                        logger.warning(f"Keepalive ping failed / 保活 ping 失败: {e}")
                    await asyncio.sleep(60)

        def _start_keepalive():
            loop = asyncio.new_event_loop()
            loop.run_until_complete(_keepalive_loop())

        t = threading.Thread(target=_start_keepalive, daemon=True)
        t.start()

        def _start_digestion_preview():
            loop = asyncio.new_event_loop()
            loop.run_until_complete(digestion_planner.periodic_loop())

        digestion_thread = threading.Thread(
            target=_start_digestion_preview, daemon=True
        )
        digestion_thread.start()

        # --- Add CORS middleware so remote clients (Cloudflare Tunnel / ngrok) can connect ---
        # --- 添加 CORS 中间件，让远程客户端（Cloudflare Tunnel / ngrok）能正常连接 ---
        if transport == "streamable-http":
            _app = mcp.streamable_http_app()
        else:
            _app = mcp.sse_app()
        original_lifespan = _app.router.lifespan_context

        @asynccontextmanager
        async def _combined_lifespan(app):
            async with original_lifespan(app):
                settlement_task = asyncio.create_task(_xinchao_settlement_loop())
                embedding_task = asyncio.create_task(
                    bucket_mgr.embedding_index.worker_loop()
                )
                task_retention_task = asyncio.create_task(_task_retention_loop())
                try:
                    yield
                finally:
                    settlement_task.cancel()
                    embedding_task.cancel()
                    task_retention_task.cancel()
                    with suppress(asyncio.CancelledError):
                        await settlement_task
                    with suppress(asyncio.CancelledError):
                        await embedding_task
                    with suppress(asyncio.CancelledError):
                        await task_retention_task

        _app.router.lifespan_context = _combined_lifespan
        _app.add_middleware(
            CORSMiddleware,
            allow_origins=["*"],
            allow_methods=["*"],
            allow_headers=["*"],
            expose_headers=["*"],
        )
        _app.add_middleware(
            MCPRequestDiagnosticsMiddleware,
            activity_callback=_observe_mcp_activity,
        )
        logger.info("CORS middleware enabled for remote transport / 已启用 CORS 中间件")
        uvicorn.run(_app, host="0.0.0.0", port=8000)
    else:
        mcp.run(transport=transport)
