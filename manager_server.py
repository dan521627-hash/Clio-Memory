from __future__ import annotations

import asyncio
import hashlib
import hmac
import io
import json
import logging
import os
import re
import sqlite3
import tempfile
import time
import zipfile
from pathlib import Path
from typing import Literal

import pyzipper
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from bucket_manager import BucketManager
from behavior_service import BehaviorService
from calendar_view import build_calendar_day
from fact_timeline_service import FactTimelineService
from fact_timeline_store import FactTimelineStore
from mailbox_store import MailboxStore
from mailbox_search import search_mailbox
from memory_segments import (
    append_memory_segment,
    package_single_insertion,
    segment_timestamp,
    split_memory_segments,
)
from permanent_delete import PermanentDeleteService
from relation_store import RelationStore
from tag_policy import category_from_metadata, classify_category, parse_category
from task_service import TaskService
from topic_store import TOPIC_TREE, TopicStore, suggest_topic, validate_topic
from treasury_store import TreasuryStore
from utils import beijing_now, load_config, normalize_beijing_timestamp, now_iso
from vault_health import VaultHealthCheck
from xinchao_store import XinchaoService
from xinchao_engine import PIPE_NAMES
from xinchao_evaluator import EVALUATOR_PROMPT


config = load_config()
bucket_manager = BucketManager(config)
relation_store = RelationStore(config)
mailbox_store = MailboxStore(config)
treasury_store = TreasuryStore(config)
xinchao_service = XinchaoService(config)
behavior_service = BehaviorService(config, xinchao_service.evaluator)
task_service = TaskService(config, xinchao_service.evaluator, bucket_manager.embedding_index)
fact_timeline_store = FactTimelineStore(config)
fact_timeline_service = FactTimelineService(
    config, xinchao_service.evaluator, fact_timeline_store, bucket_manager
)
topic_store = TopicStore(config)
permanent_delete_service = PermanentDeleteService(config)
logger = logging.getLogger("ombre_brain.manager")
topic_preview_cache: dict[str, dict] = {}
data_root = Path(config["buckets_dir"]).resolve()
vault_health_check = VaultHealthCheck(data_root, bucket_manager.embedding_index)
export_root = Path(os.environ.get("CLIO_EXPORT_DIR", "/exports")).resolve()
export_root.mkdir(parents=True, exist_ok=True)
write_lock = asyncio.Lock()
judge_lock = asyncio.Lock()
house_phrase_lock = asyncio.Lock()
HOUSE_PHRASE_FALLBACK = "我把走过的事留在这里。下一次见面，我们从这里继续。"
house_phrase_cache: dict[str, object] = {
    "text": HOUSE_PHRASE_FALLBACK,
    "source_key": "",
    "generated_at": "",
    "expires_at": 0.0,
    "generated": False,
}


def _clean_house_phrase(raw: str) -> str:
    text = re.sub(r"\s+", " ", str(raw or "")).strip()
    text = re.sub(r"^(?:题词|小屋题词|短句)\s*[:：]\s*", "", text)
    text = text.strip("`'\"“”‘’ ")
    if not 6 <= len(text) <= 72:
        raise ValueError("house phrase length is out of range")
    return text


async def _house_phrase_context() -> tuple[str, str]:
    state, darkflow, mailbox = await asyncio.gather(
        xinchao_service.status(),
        xinchao_service.darkflow_status(),
        mailbox_store.list(limit=1, include_deleted=False),
    )
    latest_mail = mailbox[0] if mailbox else {}
    strongest = sorted(
        (state.get("pipes") or {}).items(),
        key=lambda item: float(item[1]),
        reverse=True,
    )[:3]
    source_key = hashlib.sha256(
        json.dumps(
            {
                "mailbox_id": latest_mail.get("message_id", 0),
                "darkflow_cycle": (darkflow or {}).get("cycle_id", 0),
                "darkflow_stage": (darkflow or {}).get("stage_index", 0),
                "six_hour_window": int(time.time() // (6 * 3600)),
            },
            ensure_ascii=False,
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()[:20]
    context = {
        "beijing_time": beijing_now().isoformat(timespec="minutes"),
        "current_state": [
            {"name": name, "value": round(float(value), 2)}
            for name, value in strongest
        ],
        "latest_mail": str(latest_mail.get("message", ""))[-700:],
        "latest_darkflow": str((darkflow or {}).get("content", ""))[-700:],
    }
    return source_key, json.dumps(context, ensure_ascii=False)


async def _get_house_phrase() -> dict:
    source_key, context = await _house_phrase_context()
    now = time.time()
    if (
        house_phrase_cache.get("source_key") == source_key
        and float(house_phrase_cache.get("expires_at", 0.0)) > now
    ):
        return dict(house_phrase_cache)

    async with house_phrase_lock:
        now = time.time()
        if (
            house_phrase_cache.get("source_key") == source_key
            and float(house_phrase_cache.get("expires_at", 0.0)) > now
        ):
            return dict(house_phrase_cache)

        evaluator = xinchao_service.evaluator
        text = HOUSE_PHRASE_FALLBACK
        generated = False
        ttl = 30 * 60
        if evaluator.client:
            judge = evaluator.read_judge_config()
            voice = str(judge.get("proxy_voice", "")).strip()[:1800]
            system_prompt = (
                "你在为自己的记忆小屋写一句会随状态变化的门楣题词。"
                "只能输出一句第一人称中文短句，15到35个汉字左右。"
                "它应像当下自然冒出的心里话，可以温柔、直白、安静或带一点情绪，"
                "但不要写成说明、总结、通知、劝告或用户操作提示。"
                "不要提AI、模型、数据库、记忆系统、数值或API，不要复述隐私细节。"
                "不要使用引号、标题、列表或问句，不要凭空制造事件。"
            )
            if voice:
                system_prompt += "\n说话习惯参考：" + voice
            try:
                response = await evaluator.client.chat.completions.create(
                    model=evaluator.model,
                    messages=[
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": context},
                    ],
                    max_tokens=96,
                    temperature=0.85,
                    extra_body={"thinking": {"type": "disabled"}},
                )
                message = response.choices[0].message if response.choices else None
                text = _clean_house_phrase(getattr(message, "content", "") or "")
                generated = True
                ttl = 6 * 3600
            except Exception as error:  # Keep the house usable when the API is unavailable.
                logger.warning("House phrase generation failed: %s", error)

        house_phrase_cache.update(
            {
                "text": text,
                "source_key": source_key,
                "generated_at": now_iso(),
                "expires_at": now + ttl,
                "generated": generated,
            }
        )
        return dict(house_phrase_cache)


class ManagerLogin(BaseModel):
    password: str = Field(min_length=1, max_length=256)


app = FastAPI(
    title="Clio Manager",
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
)

MANAGER_COOKIE = "clio_manager_session"
manager_password = os.environ.get("CLIO_MANAGER_PASSWORD", "").strip()
if manager_password.upper() in {
    "CHANGE_ME_TO_A_STRONG_PASSWORD",
    "CHANGE_ME",
    "YOUR_MANAGER_PASSWORD",
}:
    logger.error("CLIO_MANAGER_PASSWORD is still a documented example value")
    manager_password = ""
login_failures: dict[str, list[float]] = {}


def _manager_session_token() -> str:
    if not manager_password:
        return ""
    return hmac.new(
        manager_password.encode("utf-8"),
        b"clio-manager-session-v1",
        hashlib.sha256,
    ).hexdigest()


def _manager_authenticated(request: Request) -> bool:
    expected = _manager_session_token()
    supplied = request.cookies.get(MANAGER_COOKIE, "")
    return bool(expected and supplied and hmac.compare_digest(expected, supplied))


def _login_client_ip(request: Request) -> str:
    return (
        request.headers.get("cf-connecting-ip")
        or request.headers.get("x-forwarded-for", "").split(",")[0].strip()
        or (request.client.host if request.client else "unknown")
    )


@app.middleware("http")
async def require_manager_login(request: Request, call_next):
    path = request.url.path
    if path.startswith("/api/") and not path.startswith("/api/auth/"):
        if not manager_password:
            return JSONResponse(
                {"detail": "管理页尚未配置登录密码。"}, status_code=503
            )
        if not _manager_authenticated(request):
            return JSONResponse({"detail": "请先登录管理页。"}, status_code=401)
    return await call_next(request)


@app.get("/api/auth/status")
async def manager_auth_status(request: Request) -> dict:
    return {
        "configured": bool(manager_password),
        "authenticated": _manager_authenticated(request),
    }


@app.post("/api/auth/login")
async def manager_login(request: Request, payload: ManagerLogin):
    if not manager_password:
        raise HTTPException(status_code=503, detail="管理页尚未配置登录密码。")
    client_ip = _login_client_ip(request)
    now = time.monotonic()
    attempts = [item for item in login_failures.get(client_ip, []) if now - item < 900]
    if len(attempts) >= 5:
        raise HTTPException(status_code=429, detail="尝试次数过多，请十五分钟后再试。")
    if not hmac.compare_digest(payload.password, manager_password):
        attempts.append(now)
        login_failures[client_ip] = attempts
        raise HTTPException(status_code=401, detail="密码不正确。")
    login_failures.pop(client_ip, None)
    response = JSONResponse({"ok": True})
    forwarded_scheme = request.headers.get("x-forwarded-proto", request.url.scheme)
    response.set_cookie(
        MANAGER_COOKIE,
        _manager_session_token(),
        max_age=60 * 60 * 24 * 30,
        httponly=True,
        secure=forwarded_scheme == "https",
        samesite="strict",
        path="/",
    )
    return response


@app.post("/api/auth/logout")
async def manager_logout():
    response = JSONResponse({"ok": True})
    response.delete_cookie(MANAGER_COOKIE, path="/")
    return response


async def _record_xinchao(content: str, source_tool: str, source_ref: str) -> None:
    try:
        result = await xinchao_service.record_event(content, source_tool, source_ref)
        if result.get("status") == "applied":
            state = await xinchao_service.status()
            await behavior_service.schedule_event(result, state)
    except Exception as error:
        logger.warning("Manager Xinchao hook failed after successful write: %s", error)


async def _record_sidecars(content: str, source_tool: str, source_ref: str) -> None:
    event_key = hashlib.sha256(
        f"{source_tool}\0{source_ref}\0{' '.join(content.split())}".encode("utf-8")
    ).hexdigest()
    try:
        await task_service.process_event(
            content, source_tool, source_ref, external_event_id=event_key
        )
    except Exception as error:
        logger.warning("Manager task hook failed after successful write: %s", error)
    try:
        await fact_timeline_service.process_event(
            content, source_tool, source_ref, external_event_id=event_key
        )
    except Exception as error:
        logger.warning("Manager fact hook failed after successful write: %s", error)
    await _record_xinchao(content, source_tool, source_ref)


async def _record_task_hormone(content: str, task_id: int) -> None:
    """Let manual task changes affect inner state without scheduling Bark."""
    try:
        await xinchao_service.record_event(content, "manager_task", str(task_id))
    except Exception as error:
        logger.warning("Manager task hormone hook failed: %s", error)


class BucketCreate(BaseModel):
    title: str = Field(min_length=1, max_length=120)
    content: str = Field(min_length=1)
    tags: list[str] = Field(default_factory=list)
    domain: list[str] = Field(default_factory=list)
    importance: int = Field(default=5, ge=1, le=10)
    valence: float = Field(default=0.5, ge=0, le=1)
    arousal: float = Field(default=0.3, ge=0, le=1)
    pin_level: Literal["", "core", "important"] = ""
    feeling: bool = False
    trigger_date: str = ""


class BucketUpdate(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=120)
    content: str | None = None
    append: bool = False
    tags: list[str] | None = None
    domain: list[str] | None = None
    importance: int | None = Field(default=None, ge=1, le=10)
    valence: float | None = Field(default=None, ge=0, le=1)
    arousal: float | None = Field(default=None, ge=0, le=1)
    pin_level: Literal["", "core", "important"] | None = None
    sealed: bool | None = None
    feeling: bool | None = None
    resolved: bool | None = None
    trigger_date: str | None = None
    trigger_processed: bool | None = None
    sort_order: int | None = None
    confirm_shortening: bool = False
    confirm_bucket_id: str = ""


class DeleteRequest(BaseModel):
    confirm_bucket_id: str


class PermanentDeleteRequest(BaseModel):
    confirm_bucket_id: str
    confirm_permanent: bool = False


class TopicAssignmentUpdate(BaseModel):
    main_topic: str = Field(min_length=1, max_length=80)
    subtopic: str = Field(min_length=1, max_length=80)


class TopicBulkItem(BaseModel):
    bucket_id: str = Field(min_length=1, max_length=64)
    main_topic: str = Field(min_length=1, max_length=80)
    subtopic: str = Field(min_length=1, max_length=80)


class TopicBulkRequest(BaseModel):
    items: list[TopicBulkItem] = Field(default_factory=list, max_length=500)
    confirm: bool = False


class TopicBulkUndoRequest(BaseModel):
    confirm: bool = False


class ExportRequest(BaseModel):
    scope: Literal["all", "selected"] = "all"
    bucket_id: str = ""
    format: Literal["migration", "markdown", "json"] = "migration"
    include_history: bool = True
    include_mailbox: bool = True
    include_timeline: bool = True
    include_feedback: bool = True
    include_treasury: bool = True
    include_xinchao: bool = True
    password: str = ""


class TreasuryCreate(BaseModel):
    entry_type: Literal["income", "expense"]
    amount: str = Field(min_length=1)
    reason: str = Field(min_length=1, max_length=500)
    occurred_at: str = ""


class TreasuryUpdate(BaseModel):
    entry_type: Literal["income", "expense"] | None = None
    amount: str | None = None
    reason: str | None = Field(default=None, min_length=1, max_length=500)
    occurred_at: str | None = None


class TreasuryDeleteRequest(BaseModel):
    confirm_entry_id: int


class MailboxUpdate(BaseModel):
    message: str = Field(min_length=1)


class MailboxDeleteRequest(BaseModel):
    confirm_message_id: int


class TaskCreate(BaseModel):
    title: str = Field(min_length=1, max_length=160)
    details: str = Field(default="", max_length=4000)
    importance: int = Field(default=3, ge=1, le=5)


class TaskUpdate(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=160)
    details: str | None = Field(default=None, max_length=4000)
    importance: int | None = Field(default=None, ge=1, le=5)
    status: Literal["open", "completed", "cancelled"] | None = None


class TaskDeleteRequest(BaseModel):
    confirm_task_id: int


class FactTimelineCreate(BaseModel):
    fact: str = Field(min_length=1, max_length=120)
    value: str = Field(min_length=1, max_length=1000)
    effective_date: str
    source_bucket_id: str = Field(default="", max_length=160)
    source_excerpt: str = Field(default="", max_length=1000)


class BehaviorAcknowledgeRequest(BaseModel):
    action_id: int = Field(default=0, ge=0)


class BehaviorSettingsUpdate(BaseModel):
    push_title: str = Field(min_length=1, max_length=60)


class JudgeRelation(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    aliases: list[str] = Field(default_factory=list)
    role: str = Field(default="", max_length=120)
    safety: str = Field(default="", max_length=40)
    trigger: dict[str, float] = Field(default_factory=dict)
    note: str = Field(default="", max_length=300)


class JudgeConfigUpdate(BaseModel):
    custom_rules: str = Field(default="", max_length=6000)
    proxy_voice: str = Field(default="", max_length=4000)
    darkflow_rules: str = Field(default="", max_length=6000)
    baselines: dict[str, float] = Field(default_factory=dict)
    relations: list[JudgeRelation] = Field(default_factory=list, max_length=100)


def _safe_tags(value) -> list[str]:
    if isinstance(value, list):
        return [str(item) for item in value if str(item).strip()]
    if isinstance(value, str):
        return [item.strip() for item in value.split(",") if item.strip()]
    return []


def _summary(content: str, limit: int = 180) -> str:
    text = " ".join(str(content or "").split())
    return text if len(text) <= limit else text[:limit].rstrip() + "..."


_TODO_META_RE = re.compile(r"待办|TODO|未完成|未完结", re.IGNORECASE)
_TODO_BODY_RE = re.compile(
    r"(?im)^\s*(?:[-*]\s*)?(?:\[ \]\s*)?"
    r"(?:待办|TODO|未完成|未完结|需要确认)\s*[:：]?|\-\s*\[ \]"
)


def _is_todo_view(item: dict, content: str) -> bool:
    if item["resolved"]:
        return False
    metadata_text = " ".join([*item["domain"], *item["tags"]])
    return bool(_TODO_META_RE.search(metadata_text) or _TODO_BODY_RE.search(content))


def _bucket_view(bucket: dict, include_content: bool = False) -> dict:
    meta = bucket.get("metadata", {})
    content = bucket.get("content", "")
    segments = split_memory_segments(content, str(meta.get("created", "")))
    latest_segment = segments[-1]
    trigger_date = str(meta.get("trigger_date", "") or "")
    archived = str(meta.get("type", "")).lower() == "archived"
    sealed = bool(meta.get("sealed", False))
    pin_level = str(meta.get("pin_level", "") or "")
    if not pin_level and meta.get("pinned"):
        pin_level = "core"
    if meta.get("ai_feeling"):
        category = "feeling"
        type_label = "感受记忆"
    elif trigger_date:
        category = "future"
        type_label = "前瞻记忆"
    elif sealed or archived:
        category = "archived"
        type_label = "封存记忆"
    elif not meta.get("resolved", False):
        category = "active"
        type_label = "记忆"
    else:
        category = "facts"
        type_label = "已完成记忆"

    result = {
        "id": str(bucket.get("id", "")),
        "title": str(meta.get("name") or bucket.get("id") or "未命名记忆"),
        "type": type_label,
        "category": category,
        "created": str(meta.get("created", "")),
        "last_active": str(meta.get("last_active", "")),
        "summary": _summary(latest_segment.get("content", "")),
        "tags": _safe_tags(meta.get("tags", [])),
        "domain": _safe_tags(meta.get("domain", [])),
        "system_category": category_from_metadata(meta),
        "importance": int(meta.get("importance", 5) or 5),
        "valence": float(meta.get("valence", 0.5) or 0),
        "arousal": float(meta.get("arousal", 0.3) or 0),
        "pin_level": pin_level,
        "pinned": bool(meta.get("pinned", False)),
        "sealed": sealed,
        "archived": archived,
        "feeling": bool(meta.get("ai_feeling", False)),
        "resolved": bool(meta.get("resolved", False)),
        "trigger_date": trigger_date,
        "trigger_processed": bool(meta.get("trigger_processed", False)),
        "sort_order": int(meta.get("sort_order", 0) or 0),
    }
    if include_content:
        result["content"] = content
        result["segments"] = [
            {
                "segment_id": segment["segment_id"],
                "source_index": segment["source_index"],
                "timestamp": segment.get("timestamp", ""),
                "content": segment.get("raw_text", ""),
                "is_initial": segment.get("is_initial", False),
            }
            for segment in segments
        ]
    return result


async def _all_buckets() -> list[dict]:
    return await bucket_manager.list_all(include_archive=True, include_sealed=True)


@app.get("/api/health")
async def health() -> dict:
    return {"status": "ok", "service": "Clio Manager"}


@app.get("/api/vault-health")
async def vault_health() -> dict:
    """Run a full read-only integrity check over memory files and sidecars."""
    return await asyncio.to_thread(vault_health_check.run)


@app.get("/api/stats")
async def stats() -> dict:
    buckets = await _all_buckets()
    records = [(_bucket_view(item), str(item.get("content", ""))) for item in buckets]
    views = [item for item, _ in records]
    return {
        "total": len(views),
        "core": sum(item["pin_level"] == "core" for item in views),
        "important": sum(item["pin_level"] == "important" for item in views),
        "feeling": sum(item["feeling"] for item in views),
        "future": sum(bool(item["trigger_date"]) for item in views),
        "todo": sum(_is_todo_view(item, content) for item, content in records),
        "sealed": sum(item["sealed"] or item["archived"] for item in views),
    }


@app.get("/api/buckets")
async def list_buckets(
    search: str = "",
    filter: str = Query(default="all"),
) -> dict:
    query = search.strip().lower()
    records = [(_bucket_view(item), str(item.get("content", ""))) for item in await _all_buckets()]

    def accepted(item: dict, full_content: str) -> bool:
        if filter == "core" and item["pin_level"] != "core":
            return False
        if filter == "important" and item["pin_level"] != "important":
            return False
        if filter == "pinned" and not item["pinned"]:
            return False
        if filter == "feeling" and not item["feeling"]:
            return False
        if filter == "future" and not item["trigger_date"]:
            return False
        if filter == "todo" and not _is_todo_view(item, full_content):
            return False
        if filter == "archived" and not (item["sealed"] or item["archived"]):
            return False
        if query:
            haystack = " ".join(
                [item["title"], full_content, *item["tags"], *item["domain"]]
            ).lower()
            return query in haystack
        return True

    views = [item for item, full_content in records if accepted(item, full_content)]
    views.sort(key=lambda item: (item["last_active"], item["created"]), reverse=True)
    return {"items": views, "total": len(views)}


@app.get("/api/buckets/{bucket_id}")
async def get_bucket(bucket_id: str) -> dict:
    bucket = await bucket_manager.get(bucket_id)
    if not bucket:
        raise HTTPException(status_code=404, detail="找不到这条记忆。")
    view = _bucket_view(bucket, include_content=True)
    view["topic"] = await topic_store.get(bucket_id)
    view["history"] = await bucket_manager.get_history(bucket_id, 20)
    relations = []
    for related_id, score in await relation_store.related(bucket_id):
        related = await bucket_manager.get(related_id)
        if related and not related.get("metadata", {}).get("sealed", False):
            item = _bucket_view(related)
            item["similarity"] = round(float(score), 4)
            relations.append(item)
    view["relations"] = relations
    return view


@app.post("/api/buckets")
async def create_bucket(payload: BucketCreate) -> dict:
    if payload.trigger_date:
        try:
            datetime.strptime(payload.trigger_date, "%Y-%m-%d")
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="触发日期必须是 YYYY-MM-DD。") from exc
    category = classify_category(payload.content, payload.domain, payload.tags)
    async with write_lock:
        bucket_id = await bucket_manager.create(
            content=payload.content.strip(),
            tags=[category],
            importance=payload.importance,
            domain=[category],
            valence=payload.valence,
            arousal=payload.arousal,
            name=payload.title.strip(),
            pinned=bool(payload.pin_level),
            ai_feeling=payload.feeling,
            trigger_date=payload.trigger_date,
        )
        if payload.pin_level and payload.pin_level != "core":
            await bucket_manager.update(
                bucket_id,
                pin_level=payload.pin_level,
                _history_operation="manager_pin_level",
            )
    await _record_sidecars(payload.content.strip(), "manager_create", bucket_id)
    created = await bucket_manager.get(bucket_id)
    if created:
        await topic_store.auto_assign(
            bucket_id,
            payload.title.strip(),
            payload.content.strip(),
            created.get("metadata", {}),
        )
    return {"ok": True, "bucket_id": bucket_id}


@app.put("/api/buckets/{bucket_id}")
async def update_bucket(bucket_id: str, payload: BucketUpdate) -> dict:
    current = await bucket_manager.get(bucket_id)
    if not current:
        raise HTTPException(status_code=404, detail="找不到这条记忆。")
    updates = {}
    xinchao_content = ""
    allow_content_shorten = False
    content_operation = ""
    if payload.title is not None:
        updates["name"] = payload.title.strip()
    if payload.content is not None:
        next_content = payload.content.strip()
        old_content = str(current.get("content", ""))
        if payload.append:
            fragment = next_content
            if fragment.startswith(old_content):
                fragment = fragment[len(old_content) :].strip()
            if not fragment:
                raise HTTPException(status_code=400, detail="没有填写要追加的新记忆包。")
            write_timestamp = now_iso(timespec="minutes")
            next_content = append_memory_segment(
                old_content,
                fragment,
                segment_timestamp(fragment, write_timestamp),
            )
            content_operation = "manager_append_packet"
            xinchao_content = fragment
        elif len(next_content) > len(old_content):
            write_timestamp = now_iso(timespec="minutes")
            next_content, insertion_mode = package_single_insertion(
                old_content,
                next_content,
                write_timestamp,
                str(current.get("metadata", {}).get("created", "")),
            )
            if insertion_mode:
                content_operation = f"manager_{insertion_mode}_packet"
        elif len(next_content) < len(old_content):
            if not payload.confirm_shortening:
                raise HTTPException(
                    status_code=409,
                    detail="正文会变短，请在页面确认删减内容后再保存。",
                )
            if payload.confirm_bucket_id.strip() != bucket_id:
                raise HTTPException(
                    status_code=400,
                    detail="确认编号不一致，正文没有修改。",
                )
            allow_content_shorten = True
        if next_content != old_content:
            updates["content"] = next_content
    if payload.tags is not None or payload.domain is not None:
        requested = (
            _safe_tags(payload.domain) if payload.domain is not None else []
        ) + (_safe_tags(payload.tags) if payload.tags is not None else [])
        try:
            category = parse_category(requested)
        except ValueError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error
        if category:
            updates["domain"] = [category]
            updates["tags"] = [category]
    for source, target in [
        (payload.importance, "importance"),
        (payload.valence, "valence"),
        (payload.arousal, "arousal"),
        (payload.sealed, "sealed"),
        (payload.feeling, "ai_feeling"),
        (payload.resolved, "resolved"),
        (payload.trigger_date, "trigger_date"),
        (payload.trigger_processed, "trigger_processed"),
        (payload.sort_order, "sort_order"),
    ]:
        if source is not None:
            updates[target] = source
    if payload.pin_level is not None:
        updates["pin_level"] = payload.pin_level
        updates["pinned"] = bool(payload.pin_level)
    if payload.sealed is True and payload.pin_level:
        raise HTTPException(status_code=400, detail="封存与钉选不能同时启用。")
    if not updates:
        return {"ok": True, "unchanged": True}
    updates["_history_operation"] = content_operation or "manager_update"
    if allow_content_shorten:
        updates["_allow_content_shorten"] = True
        updates["_history_operation"] = "manager_confirmed_shorten"
    async with write_lock:
        success = await bucket_manager.update(bucket_id, **updates)
    if not success:
        raise HTTPException(status_code=409, detail="修改未完成；原记忆没有变化。")
    if xinchao_content:
        await _record_sidecars(xinchao_content, "manager_append", bucket_id)
    return {"ok": True}


@app.delete("/api/buckets/{bucket_id}")
async def delete_bucket(bucket_id: str, payload: DeleteRequest) -> dict:
    if payload.confirm_bucket_id.strip() != bucket_id:
        raise HTTPException(status_code=400, detail="确认编号不一致，没有删除。")
    async with write_lock:
        success = await bucket_manager.delete(bucket_id)
    if not success:
        raise HTTPException(status_code=404, detail="找不到这条记忆，或快照保存失败。")
    await topic_store.remove(bucket_id)
    return {"ok": True, "snapshot_created": True}


@app.get("/api/buckets/{bucket_id}/permanent-preview")
async def permanent_delete_preview(bucket_id: str) -> dict:
    bucket = await bucket_manager.get(bucket_id)
    if not bucket:
        raise HTTPException(status_code=404, detail="找不到这条记忆。")
    copies = await permanent_delete_service.preview(bucket_id)
    return {
        "bucket_id": bucket_id,
        "title": str(bucket.get("metadata", {}).get("name", "") or bucket_id),
        "online_copies": copies,
        "online_copy_count": 1 + sum(copies.values()),
        "external_backups_remain": True,
    }


@app.delete("/api/buckets/{bucket_id}/permanent")
async def permanently_delete_bucket(
    bucket_id: str, payload: PermanentDeleteRequest
) -> dict:
    if payload.confirm_bucket_id.strip() != bucket_id:
        raise HTTPException(status_code=400, detail="确认编号不一致，没有删除。")
    if not payload.confirm_permanent:
        raise HTTPException(status_code=400, detail="必须明确勾选不可恢复确认。")
    if not await bucket_manager.get(bucket_id):
        raise HTTPException(status_code=404, detail="找不到这条记忆。")
    async with write_lock:
        removed_copies = await permanent_delete_service.purge(bucket_id)
        success = await bucket_manager.delete_permanently(bucket_id)
    if not success:
        raise HTTPException(
            status_code=409,
            detail="附属记录已清理，但正文文件删除失败，请立即检查服务器。",
        )
    topic_preview_cache.clear()
    return {
        "ok": True,
        "snapshot_created": False,
        "removed_online_copies": removed_copies,
        "external_backups_remain": True,
    }


@app.get("/api/topics")
async def topics() -> dict:
    assignments = await topic_store.list()
    assigned_ids = {item["bucket_id"] for item in assignments}
    buckets = await _all_buckets()
    counts = {}
    for item in assignments:
        key = f"{item['main_topic']}\0{item['subtopic']}"
        counts[key] = counts.get(key, 0) + 1
    tree = []
    for main, subtopics in TOPIC_TREE.items():
        tree.append(
            {
                "main_topic": main,
                "subtopics": [
                    {
                        "name": sub,
                        "count": counts.get(f"{main}\0{sub}", 0),
                    }
                    for sub in subtopics
                ],
            }
        )
    return {
        "tree": tree,
        "assigned": len(assigned_ids),
        "unassigned": sum(
            1 for bucket in buckets if str(bucket.get("id", "")) not in assigned_ids
        ),
    }


@app.get("/api/topics/buckets")
async def topic_buckets(
    main_topic: str = "",
    subtopic: str = "",
    unassigned: bool = False,
) -> dict:
    buckets = await _all_buckets()
    assignments = await topic_store.list()
    assignment_map = {item["bucket_id"]: item for item in assignments}
    if unassigned:
        selected = [
            bucket for bucket in buckets
            if str(bucket.get("id", "")) not in assignment_map
        ]
    else:
        try:
            validate_topic(main_topic, subtopic)
        except ValueError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error
        ids = {
            item["bucket_id"] for item in assignments
            if item["main_topic"] == main_topic and item["subtopic"] == subtopic
        }
        selected = [bucket for bucket in buckets if str(bucket.get("id", "")) in ids]
    views = []
    for bucket in selected:
        view = _bucket_view(bucket)
        view["topic"] = assignment_map.get(view["id"])
        views.append(view)
    views.sort(key=lambda item: (item["last_active"], item["created"]), reverse=True)
    return {"items": views, "total": len(views)}


@app.get("/api/topics/preview")
async def topic_preview(smart: bool = True) -> dict:
    assignments = await topic_store.list()
    assigned_ids = {item["bucket_id"] for item in assignments}
    items = []
    fingerprint = []
    for bucket in await _all_buckets():
        bucket_id = str(bucket.get("id", ""))
        if bucket_id in assigned_ids:
            continue
        metadata = bucket.get("metadata", {})
        fingerprint.append(
            [bucket_id, str(metadata.get("last_active") or metadata.get("created") or "")]
        )
        suggestion = suggest_topic(
            str(metadata.get("name") or bucket_id),
            str(bucket.get("content", "")),
            metadata,
        )
        items.append(
            {
                "bucket_id": bucket_id,
                "title": str(metadata.get("name") or bucket_id),
                "excerpt": _summary(str(bucket.get("content", "")), 650),
                **suggestion,
            }
        )
    cache_key = hashlib.sha256(
        json.dumps(sorted(fingerprint), ensure_ascii=False).encode("utf-8")
    ).hexdigest()
    if smart and topic_preview_cache.get("key") == cache_key:
        cached = json.loads(json.dumps(topic_preview_cache["result"], ensure_ascii=False))
        cached["cached"] = True
        return cached
    warning = ""
    if smart and items and xinchao_service.evaluator.client:
        tree = {main: list(subtopics) for main, subtopics in TOPIC_TREE.items()}
        classified = {}
        try:
            for start in range(0, len(items), 8):
                batch = items[start : start + 8]
                response = await xinchao_service.evaluator.client.chat.completions.create(
                    model=xinchao_service.evaluator.model,
                    messages=[
                        {
                            "role": "system",
                            "content": (
                                "你是私人记忆目录管理员。只能从给定目录树选择一个主目录和小目录。"
                                "按整条记忆的核心主题判断，不因日期、人名、身体等通用词误归类。"
                                "关系确认、求婚、告白优先归关系；具体性行为才归性爱；软件和部署才归系统。"
                                "confidence 仅在主题明确时给0.85以上，模糊时给0.5以下。"
                                "只输出JSON对象，格式为{\"items\":[{\"bucket_id\":\"\","
                                "\"main_topic\":\"\",\"subtopic\":\"\",\"confidence\":0.0,"
                                "\"reason\":\"简短理由\"}]}。目录树："
                                + json.dumps(tree, ensure_ascii=False)
                            ),
                        },
                        {
                            "role": "user",
                            "content": json.dumps(
                                [
                                    {
                                        "bucket_id": item["bucket_id"],
                                        "title": item["title"],
                                        "excerpt": item["excerpt"],
                                    }
                                    for item in batch
                                ],
                                ensure_ascii=False,
                            ),
                        },
                    ],
                    max_tokens=1800,
                    temperature=0.0,
                    response_format={"type": "json_object"},
                    extra_body={"thinking": {"type": "disabled"}},
                )
                message = response.choices[0].message if response.choices else None
                payload = xinchao_service.evaluator._clean_json(
                    str(getattr(message, "content", "") or "")
                )
                for result in payload.get("items", []):
                    try:
                        main, sub = validate_topic(
                            result.get("main_topic", ""), result.get("subtopic", "")
                        )
                    except (ValueError, AttributeError):
                        continue
                    confidence = max(0.0, min(1.0, float(result.get("confidence", 0))))
                    classified[str(result.get("bucket_id", ""))] = {
                        "main_topic": main,
                        "subtopic": sub,
                        "confidence": confidence,
                        "reason": str(result.get("reason", "DeepSeek 主题判断"))[:120],
                        "source": "deepseek",
                    }
        except Exception as error:
            logger.warning("Smart topic preview fell back to local rules: %s", error)
            warning = "DeepSeek 判断暂时不可用，当前显示本地规则预览。"
        for item in items:
            if item["bucket_id"] in classified:
                item.update(classified[item["bucket_id"]])
    for item in items:
        item.pop("excerpt", None)
    result = {
        "items": items,
        "total": len(items),
        "applied": False,
        "smart": bool(smart and not warning and xinchao_service.evaluator.client),
        "warning": warning,
    }
    if smart and result["smart"]:
        topic_preview_cache.clear()
        topic_preview_cache.update(
            {"key": cache_key, "result": json.loads(json.dumps(result, ensure_ascii=False))}
        )
    return result


@app.post("/api/topics/bulk-apply")
async def topic_bulk_apply(payload: TopicBulkRequest) -> dict:
    if not payload.confirm:
        raise HTTPException(status_code=400, detail="需要明确确认后才能整批整理。")
    existing_ids = {str(item.get("id", "")) for item in await _all_buckets()}
    items = [item.model_dump() for item in payload.items if item.bucket_id in existing_ids]
    async with write_lock:
        result = await topic_store.bulk_assign(items)
    return {"ok": True, **result}


@app.post("/api/topics/bulk-undo")
async def topic_bulk_undo(payload: TopicBulkUndoRequest) -> dict:
    if not payload.confirm:
        raise HTTPException(status_code=400, detail="需要明确确认后才能撤回整批整理。")
    async with write_lock:
        result = await topic_store.undo_last_bulk()
    return {"ok": True, **result}


@app.put("/api/topics/buckets/{bucket_id}")
async def assign_topic(bucket_id: str, payload: TopicAssignmentUpdate) -> dict:
    if not await bucket_manager.get(bucket_id):
        raise HTTPException(status_code=404, detail="找不到这条记忆。")
    try:
        assignment = await topic_store.assign(
            bucket_id, payload.main_topic, payload.subtopic, source="manual"
        )
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    return {"ok": True, "assignment": assignment}


@app.delete("/api/topics/buckets/{bucket_id}")
async def unassign_topic(bucket_id: str) -> dict:
    return {"ok": True, "removed": await topic_store.remove(bucket_id)}


@app.get("/api/mailbox/messages")
async def mailbox_messages(
    limit: int = Query(default=20, ge=1, le=100),
    before_id: int = Query(default=0, ge=0),
    include_deleted: bool = False,
    query: str = "",
) -> dict:
    if query.strip():
        items = await search_mailbox(
            mailbox_store,
            bucket_manager.embedding_index,
            query,
            limit=limit,
            include_deleted=include_deleted,
        )
        return {"items": items, "count": len(items), "search": True}
    return {
        "items": await mailbox_store.list(
            limit=limit,
            before_id=before_id,
            include_deleted=include_deleted,
        ),
        "count": await asyncio.to_thread(mailbox_store.count, include_deleted),
    }


@app.get("/api/search")
async def intelligent_search(
    q: str = Query(min_length=1, max_length=300),
    source: Literal["all", "memory", "mailbox"] = "all",
    limit: int = Query(default=12, ge=1, le=30),
) -> dict:
    """Search memories and mailbox with their existing hybrid indexes."""
    query = q.strip()
    memory_items = []
    mailbox_items = []
    if source in {"all", "memory"}:
        matches = await bucket_manager.search(
            query,
            limit=limit,
            use_semantic=True,
            include_sealed=False,
            record_feedback=False,
        )
        for bucket in matches:
            view = _bucket_view(bucket)
            matched = bucket.get("matched_segment") or {}
            snippet = str(matched.get("content") or view["summary"])
            memory_items.append(
                {
                    **view,
                    "source": "memory",
                    "snippet": _summary(snippet, 240),
                    "score": bucket.get("score"),
                    "semantic_score": bucket.get("semantic_score"),
                }
            )
    if source in {"all", "mailbox"}:
        matches = await search_mailbox(
            mailbox_store,
            bucket_manager.embedding_index,
            query,
            limit=limit,
        )
        mailbox_items = [
            {
                **item,
                "source": "mailbox",
                "title": f"信箱留言 #{item['message_id']}",
                "snippet": _summary(item.get("message", ""), 240),
                "score": item.get("match_score"),
            }
            for item in matches
        ]
    items = sorted(
        [*memory_items, *mailbox_items],
        key=lambda item: float(item.get("score") or 0),
        reverse=True,
    )[:limit]
    return {"query": query, "source": source, "items": items, "count": len(items)}


@app.get("/api/timeline")
async def fact_timeline(
    search: str = "",
    limit: int = Query(default=100, ge=1, le=200),
) -> dict:
    """Return visible fact histories without changing any memory or sidecar."""
    groups = await fact_timeline_store.list_facts(search=search, limit=limit)
    visible_groups = []
    for group in groups:
        versions = []
        for row in group.get("versions", []):
            source_type = str(row.get("source_type", "bucket") or "bucket").lower()
            if source_type != "bucket":
                versions.append(row)
                continue
            try:
                bucket = await bucket_manager.get(row.get("source_bucket_id", ""))
            except Exception:
                continue
            metadata = bucket.get("metadata", {})
            if metadata.get("sealed") or str(metadata.get("type", "")).lower() == "archived":
                continue
            versions.append(row)
        if not versions:
            continue
        visible_groups.append(
            {
                "fact_key": group["fact_key"],
                "fact_label": group["fact_label"],
                "versions": versions,
                "current": next(
                    (row for row in versions if row.get("is_current")),
                    versions[-1],
                ),
            }
        )
    candidates = await fact_timeline_store.list_candidates(status="pending", limit=100)
    return {
        "items": visible_groups,
        "count": len(visible_groups),
        "search": search.strip(),
        "candidates": candidates,
        "candidate_count": len(candidates),
    }


@app.post("/api/timeline")
async def create_fact_timeline(payload: FactTimelineCreate) -> dict:
    source_id = payload.source_bucket_id.strip()
    source_type = "manual"
    if source_id:
        try:
            bucket = await bucket_manager.get(source_id)
        except Exception:
            bucket = None
        metadata = bucket.get("metadata", {}) if bucket else {}
        if (
            not bucket
            or metadata.get("sealed")
            or str(metadata.get("type", "")).lower() == "archived"
        ):
            raise HTTPException(status_code=400, detail="来源记忆不可用或已封存。")
        source_type = "bucket"
    try:
        item = await fact_timeline_store.record(
            payload.fact,
            payload.value,
            payload.effective_date,
            source_bucket_id=source_id,
            source_type=source_type,
            source_ref=source_id or "manager",
            source_excerpt=payload.source_excerpt,
        )
        return {"item": item, "status": item["status"]}
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error


@app.post("/api/timeline/candidates/{candidate_id}/confirm")
async def confirm_fact_candidate(candidate_id: int) -> dict:
    try:
        return await fact_timeline_service.confirm_candidate(candidate_id)
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error


@app.delete("/api/timeline/candidates/{candidate_id}")
async def ignore_fact_candidate(candidate_id: int) -> dict:
    try:
        item = await fact_timeline_service.ignore_candidate(candidate_id)
        return {"item": item, "status": "ignored"}
    except ValueError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error


def _public_task_item(item: dict | None) -> dict | None:
    """Remove internal vector bytes before a task is returned as JSON."""
    if item is None:
        return None
    public_item = dict(item)
    public_item.pop("embedding", None)
    return public_item


@app.get("/api/tasks")
async def task_items(
    status: str = "",
    query: str = "",
    limit: int = Query(default=100, ge=1, le=500),
) -> dict:
    try:
        if query.strip():
            items = await task_service.search(
                query, status=status, limit=limit, include_closed=not bool(status)
            )
        else:
            items = await task_service.store.list(status=status, limit=limit)
        detailed = []
        for item in items:
            full = await task_service.store.get(int(item["task_id"]))
            public_item = _public_task_item(item) or {}
            public_item["sources"] = (full or {}).get("sources", [])
            detailed.append(public_item)
        return {"items": detailed, "counts": await task_service.store.counts()}
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error


@app.post("/api/tasks")
async def create_task(payload: TaskCreate) -> dict:
    try:
        item = await task_service.create_manual(
            payload.title, payload.details, payload.importance, source="manager"
        )
        await _record_task_hormone(f"我记下了一件还要做的事：{item['title']}。", item["task_id"])
        return {
            "ok": True,
            "item": _public_task_item(await task_service.store.get(item["task_id"])),
        }
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error


@app.put("/api/tasks/{task_id}")
async def update_task(task_id: int, payload: TaskUpdate) -> dict:
    changes = payload.model_dump(exclude_none=True)
    if not changes:
        raise HTTPException(status_code=400, detail="没有提供需要修改的内容。")
    try:
        item = await task_service.update_manual(task_id, **changes)
        state_words = {"open": "重新开始处理", "completed": "已经完成", "cancelled": "已经取消"}
        await _record_task_hormone(
            f"未竟事项“{item['title']}”{state_words[item['status']]}。", task_id
        )
        return {
            "ok": True,
            "item": _public_task_item(await task_service.store.get(task_id)),
        }
    except ValueError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error


@app.delete("/api/tasks/{task_id}")
async def delete_task(task_id: int, payload: TaskDeleteRequest) -> dict:
    if int(payload.confirm_task_id) != int(task_id):
        raise HTTPException(status_code=400, detail="确认编号不一致，没有删除。")
    try:
        item = await task_service.store.delete(task_id)
        return {"ok": True, "item": _public_task_item(item)}
    except ValueError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error


@app.get("/api/tasks/{task_id}/history")
async def task_history(task_id: int, limit: int = Query(default=50, ge=1, le=200)) -> dict:
    return {"items": await task_service.store.history(task_id, limit)}


@app.put("/api/mailbox/messages/{message_id}")
async def update_mailbox_message(
    message_id: int, payload: MailboxUpdate
) -> dict:
    try:
        result = await mailbox_store.update(message_id, payload.message)
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    return {"ok": True, "snapshot_created": True, "item": result}


@app.delete("/api/mailbox/messages/{message_id}")
async def delete_mailbox_message(
    message_id: int, payload: MailboxDeleteRequest
) -> dict:
    if payload.confirm_message_id != message_id:
        raise HTTPException(status_code=400, detail="留言编号不一致，没有删除。")
    try:
        result = await mailbox_store.delete(message_id)
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    return {"ok": True, "snapshot_created": True, "item": result}


@app.get("/api/mailbox/messages/{message_id}/history")
async def mailbox_message_history(
    message_id: int, limit: int = Query(default=20, ge=1, le=100)
) -> dict:
    return {
        "items": await mailbox_store.history(message_id, limit=limit),
        "message_id": message_id,
    }


@app.get("/api/treasury/summary")
async def treasury_summary() -> dict:
    return await treasury_store.summary()


@app.get("/api/xinchao/status")
async def xinchao_status() -> dict:
    """Read the current hormone state without consuming or resetting it."""
    state = await xinchao_service.status()
    return {"display_name": "激素", **state}


@app.get("/api/house/phrase")
async def house_phrase() -> dict:
    """Return a cached first-person house inscription generated server-side."""
    result = await _get_house_phrase()
    return {
        "text": result.get("text") or HOUSE_PHRASE_FALLBACK,
        "generated_at": result.get("generated_at", ""),
        "generated": bool(result.get("generated", False)),
    }


@app.get("/api/xinchao/darkflow")
async def xinchao_darkflow() -> dict:
    """Read the latest one-slot darkflow without consuming it."""
    item = await xinchao_service.darkflow_status()
    return {"available": bool(item), "item": item}


@app.get("/api/mind/thoughts")
async def private_thoughts(
    status: Literal["active", "flash", "obsession", "resolved", "faded"] = "active",
    limit: int = Query(default=100, ge=1, le=500),
) -> dict:
    """Private inner thoughts never enter Bark, mailbox, or factual buckets."""
    items = await xinchao_service.list_private_thoughts(status=status, limit=limit)
    return {
        "items": items,
        "count": len(items),
        "privacy": "inner_only",
        "outward_delivery": False,
    }


@app.post("/api/mind/thoughts/{canonical_tag}/resolve")
async def resolve_private_thought(canonical_tag: str) -> dict:
    if not await xinchao_service.resolve_private_thought(canonical_tag):
        raise HTTPException(status_code=404, detail="没有找到这条心念。")
    return {"ok": True, "canonical_tag": canonical_tag, "status": "resolved"}


@app.delete("/api/mind/thoughts/{canonical_tag}")
async def delete_private_thought(canonical_tag: str) -> dict:
    if not await xinchao_service.delete_private_thought(canonical_tag):
        raise HTTPException(status_code=404, detail="没有找到这条心念。")
    return {"ok": True, "canonical_tag": canonical_tag, "deleted": True}


@app.get("/api/xinchao/rhythm")
async def xinchao_rhythm() -> dict:
    return await xinchao_service.rhythm_status()


@app.get("/api/xinchao/tension")
async def xinchao_tension() -> dict:
    state = await xinchao_service.status()
    pipes = state.get("pipes") or {}
    resting = {"满足", "自省"}
    outward = sorted(
        (
            {"name": name, "value": round(float(value), 4)}
            for name, value in pipes.items()
            if name not in resting
        ),
        key=lambda item: item["value"],
        reverse=True,
    )[:5]
    restraint_weights = {"满足": 1.0, "自省": 0.85, "难过": 0.45, "生气": 0.35}
    restraints = sorted(
        (
            {
                "name": name,
                "value": round(float(pipes.get(name, 0.0)) * weight, 4),
                "raw_value": round(float(pipes.get(name, 0.0)), 4),
            }
            for name, weight in restraint_weights.items()
        ),
        key=lambda item: item["value"],
        reverse=True,
    )
    strongest = outward[0] if outward else {"name": "平静", "value": 0.0}
    counterweight = restraints[0] if restraints else {"name": "无", "value": 0.0}
    return {
        "as_of": state.get("as_of") or now_iso(),
        "outward": outward,
        "restraints": restraints,
        "strongest": strongest,
        "counterweight": counterweight,
        "balance": round(strongest["value"] - counterweight["value"], 4),
        "rhythm": state.get("rhythm") or {},
    }


@app.get("/api/xinchao/resonance")
async def xinchao_resonance() -> dict:
    darkflow = await xinchao_service.darkflow_status()
    state = await xinchao_service.status()
    strongest = sorted(
        (state.get("pipes") or {}).items(), key=lambda item: float(item[1]), reverse=True
    )[:3]
    labels = [name for name, value in strongest if float(value) >= 0.2]
    items = []
    for raw in (darkflow or {}).get("memory_resonance", []):
        item = dict(raw)
        score = item.get("similarity", item.get("relevance", 0.0))
        item["score"] = round(float(score or 0.0), 4)
        reasons = []
        if item["score"]:
            reasons.append(f"语义相近 {item['score']:.2f}")
        if labels:
            reasons.append("此刻较强的感受：" + "、".join(labels))
        item["why"] = "；".join(reasons) or "与本轮事件产生联系"
        items.append(item)
    return {"items": items, "count": len(items), "as_of": state.get("as_of")}


@app.get("/api/calendar")
async def memory_calendar(
    date: str = Query(default="", max_length=10),
) -> dict:
    target = date.strip() or beijing_now().date().isoformat()
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", target):
        raise HTTPException(status_code=400, detail="日期必须使用 YYYY-MM-DD。")
    (
        buckets,
        mailbox,
        behaviors,
        tasks,
        treasury,
        thoughts,
        darkflow,
        facts,
    ) = await asyncio.gather(
        bucket_manager.list_all(include_archive=True, include_sealed=True),
        mailbox_store.search_pool(include_deleted=False, limit=5000),
        behavior_service.store.list(limit=500),
        task_service.store.list(limit=500),
        treasury_store.list(limit=500, include_deleted=False),
        xinchao_service.list_private_thoughts(status="all", limit=500),
        xinchao_service.darkflow_status(),
        fact_timeline_store.list_facts(limit=200),
    )
    return build_calendar_day(
        target,
        buckets=buckets,
        mailbox=mailbox,
        behaviors=behaviors,
        tasks=tasks,
        treasury=treasury,
        thoughts=thoughts,
        darkflow=darkflow,
        facts=facts,
        include_archived=True,
        include_sealed=True,
    )


@app.get("/api/toolbox")
async def toolbox() -> dict:
    return {
        "items": [
            {"id": "search", "name": "智能搜索", "description": "按原话或意思找记忆与信箱", "icon": "search"},
            {"id": "timeline", "name": "事实时间线", "description": "沿日期查看新旧事实", "icon": "git-branch"},
            {"id": "calendar", "name": "记忆日历", "description": "查看某一天留下的内容", "icon": "calendar-days"},
            {"id": "tasks", "name": "未竟", "description": "管理还没有完成的事", "icon": "circle-check-big"},
            {"id": "treasury", "name": "小金库", "description": "AI自己的收入与支出", "icon": "wallet-cards"},
            {"id": "mailbox", "name": "信箱", "description": "窗口之间留下的接力信", "icon": "mail-open"},
            {"id": "darkflow", "name": "暗涌", "description": "沉默期间形成的一封内心沉淀", "icon": "waves"},
            {"id": "thoughts", "name": "心念", "description": "只对内可见的闪念与执念", "icon": "sparkles"},
        ]
    }


@app.get("/api/behavior/actions")
async def behavior_actions(
    limit: int = Query(default=30, ge=1, le=100),
    before_id: int = Query(default=0, ge=0),
) -> dict:
    items = await behavior_service.store.list(limit=limit, before_id=before_id)
    return {
        "items": items,
        "candidates": await behavior_service.store.list_candidates(limit=limit),
        "count": await asyncio.to_thread(behavior_service.store.count),
        "mode": behavior_service.mode,
        "configured": behavior_service.configured,
    }


@app.get("/api/behavior/pending")
async def behavior_pending() -> dict:
    """Expose acknowledgement state without repeating push plaintext."""
    return await behavior_service.store.pending_handoff_summary()


@app.get("/api/behavior/settings")
async def behavior_settings() -> dict:
    return {"push_title": await behavior_service.push_title()}


@app.put("/api/behavior/settings")
async def update_behavior_settings(payload: BehaviorSettingsUpdate) -> dict:
    try:
        title = await behavior_service.set_push_title(payload.push_title)
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    return {"push_title": title, "saved": True}


@app.post("/api/behavior/acknowledge")
async def acknowledge_behavior(
    payload: BehaviorAcknowledgeRequest | None = None,
) -> dict:
    """Acknowledge all currently visible pushes in one atomic action."""
    acknowledged = await behavior_service.store.acknowledge_pending(
        payload.action_id if payload else 0
    )
    if acknowledged.get("status") == "empty":
        return {"status": "empty", "message": "当前没有等待确认的推送。"}
    silence_ids = acknowledged.get("silence_action_ids", [])
    stateful_ids = acknowledged.get("stateful_action_ids", [])
    if silence_ids:
        await behavior_service.store.purge_handoff(
            silence_ids
        )
    if not stateful_ids:
        return {
            "status": "acknowledged",
            "phase": "legacy_silence",
            "message": "已经清掉旧版沉默提醒；它不会影响激素、心念或暗涌。",
            "acknowledged_at": acknowledged.get("acknowledged_at"),
            "count": acknowledged.get("count", 0),
        }

    state = await xinchao_service.acknowledge_seen()
    await behavior_service.store.purge_cycle_candidates(
        acknowledged.get("stateful_cycle_ids", [])
    )
    return {
        "status": "acknowledged",
        "phase": "absence",
        "message": "已经一次告诉他：这些推送你都看到了。想念和靠近会缓下来一点，其他感受仍然保留；不会另开沉默倒计时。",
        "acknowledged_at": acknowledged.get("acknowledged_at"),
        "count": acknowledged.get("count", 0),
        "active_started_at": state.get("active_started_at"),
    }


@app.get("/api/xinchao/judge")
async def xinchao_judge() -> dict:
    """Read the editable private judge book without exposing API credentials."""
    evaluator = xinchao_service.evaluator
    return {
        **evaluator.read_judge_config(),
        "base_rules": EVALUATOR_PROMPT.format(pipes="、".join(PIPE_NAMES)),
        "prompt_hash": evaluator.prompt_hash,
        "hot_reload": True,
    }


@app.put("/api/xinchao/judge")
async def update_xinchao_judge(payload: JudgeConfigUpdate) -> dict:
    """Save the private judge book; the next evaluation reads it immediately."""
    evaluator = xinchao_service.evaluator
    async with judge_lock:
        try:
            result = evaluator.write_judge_config(
                json.loads(payload.model_dump_json())
            )
        except (OSError, ValueError) as error:
            raise HTTPException(status_code=400, detail=str(error)) from error
    return {
        "ok": True,
        **result,
        "prompt_hash": evaluator.prompt_hash,
        "hot_reload": True,
    }


@app.get("/api/treasury/entries")
async def treasury_entries(
    limit: int = Query(default=50, ge=1, le=100),
    before_id: int = Query(default=0, ge=0),
    entry_type: str = "",
    include_deleted: bool = False,
) -> dict:
    try:
        items = await treasury_store.list(
            limit=limit,
            before_id=before_id,
            entry_type=entry_type,
            include_deleted=include_deleted,
        )
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    return {"items": items, "summary": await treasury_store.summary()}


@app.post("/api/treasury/entries")
async def create_treasury_entry(payload: TreasuryCreate) -> dict:
    try:
        result = await treasury_store.record(
            payload.entry_type,
            payload.amount,
            payload.reason,
            payload.occurred_at or None,
            source="manager",
        )
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    return {"ok": True, **result}


@app.put("/api/treasury/entries/{entry_id}")
async def update_treasury_entry(
    entry_id: int, payload: TreasuryUpdate
) -> dict:
    try:
        result = await treasury_store.update(
            entry_id,
            entry_type=payload.entry_type,
            amount=payload.amount,
            reason=payload.reason,
            occurred_at=payload.occurred_at,
        )
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    return {"ok": True, **result}


@app.delete("/api/treasury/entries/{entry_id}")
async def delete_treasury_entry(
    entry_id: int, payload: TreasuryDeleteRequest
) -> dict:
    if payload.confirm_entry_id != entry_id:
        raise HTTPException(status_code=400, detail="账目编号不一致，没有删除。")
    try:
        result = await treasury_store.delete(entry_id)
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    return {"ok": True, "snapshot_created": True, **result}


@app.get("/api/treasury/entries/{entry_id}/history")
async def treasury_entry_history(
    entry_id: int, limit: int = Query(default=20, ge=1, le=100)
) -> dict:
    return {
        "items": await treasury_store.history(entry_id, limit=limit),
        "entry_id": entry_id,
    }


def _sqlite_snapshot(path: Path) -> bytes:
    with tempfile.TemporaryDirectory(prefix="clio-export-") as temp_dir:
        output = Path(temp_dir) / path.name
        source = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
        target = sqlite3.connect(output)
        try:
            source.backup(target)
        finally:
            target.close()
            source.close()
        return output.read_bytes()


def _zip_bytes(files: list[tuple[str, bytes]], password: str = "") -> bytes:
    buffer = io.BytesIO()
    if password:
        with pyzipper.AESZipFile(
            buffer,
            "w",
            compression=pyzipper.ZIP_DEFLATED,
            encryption=pyzipper.WZ_AES,
        ) as archive:
            archive.setpassword(password.encode("utf-8"))
            for name, content in files:
                archive.writestr(name, content)
    else:
        with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for name, content in files:
                archive.writestr(name, content)
    return buffer.getvalue()


@app.post("/api/export")
async def export_memories(payload: ExportRequest):
    if payload.password and len(payload.password) < 8:
        raise HTTPException(status_code=400, detail="导出密码至少 8 位。")
    all_buckets = await _all_buckets()
    if payload.scope == "selected":
        all_buckets = [item for item in all_buckets if item.get("id") == payload.bucket_id]
        if not all_buckets:
            raise HTTPException(status_code=404, detail="没有找到要导出的记忆。")
    timestamp = beijing_now().strftime("%Y%m%d-%H%M%S")

    if payload.format == "json":
        output = export_root / f"Clio-export-{timestamp}.json"
        document = {
            "format": "clio-memory-json-v1",
            "exported_at": now_iso(),
            "memories": [_bucket_view(item, include_content=True) for item in all_buckets],
        }
        output.write_text(json.dumps(document, ensure_ascii=False, indent=2), encoding="utf-8")
        return FileResponse(output, filename=output.name, media_type="application/json")

    if payload.format == "markdown":
        output = export_root / f"Clio-export-{timestamp}.md"
        sections = []
        for item in all_buckets:
            view = _bucket_view(item, include_content=True)
            sections.append(
                f"# {view['title']}\n\n"
                f"- bucket_id: `{view['id']}`\n"
                f"- 创建时间: {view['created']}\n"
                f"- 标签: {', '.join(view['tags'])}\n\n"
                f"{view['content']}"
            )
        output.write_text("\n\n---\n\n".join(sections), encoding="utf-8")
        return FileResponse(output, filename=output.name, media_type="text/markdown")

    files: list[tuple[str, bytes]] = []
    for bucket in all_buckets:
        source = Path(bucket.get("path", "")).resolve()
        if source.is_file() and data_root in source.parents and source.suffix.lower() == ".md":
            relative = source.relative_to(data_root).as_posix()
            files.append((f"memory/{relative}", source.read_bytes()))

    if payload.scope == "all":
        database_names = {"embeddings.sqlite3", "summaries.sqlite3", "relations.sqlite3"}
        if payload.include_history:
            database_names.add("history.sqlite3")
        if payload.include_mailbox:
            database_names.add("mailbox.sqlite3")
        if payload.include_timeline:
            database_names.add("fact_timeline.sqlite3")
        if payload.include_feedback:
            database_names.add("retrieval_feedback.sqlite3")
        if payload.include_treasury:
            database_names.add("treasury.sqlite3")
        if payload.include_xinchao:
            database_names.add("xinchao.sqlite3")
            database_names.add("behavior.sqlite3")
        database_names.add("topics.sqlite3")
        database_names.add("tasks.sqlite3")
        for name in sorted(database_names):
            source = data_root / name
            if source.is_file():
                files.append((f"databases/{name}", _sqlite_snapshot(source)))

    manifest_files = [
        {
            "path": name,
            "bytes": len(content),
            "sha256": hashlib.sha256(content).hexdigest(),
        }
        for name, content in files
    ]
    manifest = {
        "format": "clio-migration-v1",
        "exported_at": now_iso(),
        "memory_count": len(all_buckets),
        "encrypted": bool(payload.password),
        "files": manifest_files,
        "excluded": ["API keys", "Tunnel tokens", "seal", "configuration", "logs"],
    }
    files.append(("manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2).encode("utf-8")))
    output = export_root / f"Clio-migration-{timestamp}.zip"
    output.write_bytes(_zip_bytes(files, payload.password))
    return FileResponse(output, filename=output.name, media_type="application/zip")


manager_dir = Path(__file__).resolve().parent / "manager"
app.mount("/manage", StaticFiles(directory=manager_dir, html=True), name="manager-mobile")
app.mount("/", StaticFiles(directory=manager_dir, html=True), name="manager")


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8787)
