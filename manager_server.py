from __future__ import annotations

import asyncio
import hashlib
import hmac
import io
import json
import logging
import os
import re
import secrets
import sqlite3
import tempfile
import time
import zipfile
from datetime import datetime
from pathlib import Path
from typing import Literal
from urllib.parse import quote, urlsplit

import httpx
import pyzipper
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from bucket_manager import BucketManager
from brain_context_service import build_brain_context
from behavior_service import BehaviorService
from calendar_view import build_calendar_day
from fact_timeline_service import FactTimelineService
from fact_timeline_store import FactTimelineStore
from mailbox_store import MailboxStore
from mailbox_search import search_mailbox
from living_memory import LivingMemoryStore
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
from xinchao_evaluator import EVALUATOR_PROMPT, PIPE_GUIDE


config = load_config()
bucket_manager = BucketManager(config)
relation_store = RelationStore(config)
mailbox_store = MailboxStore(config)
treasury_store = TreasuryStore(config)
xinchao_service = XinchaoService(config)
xinchao_service.set_thought_embedding_provider(bucket_manager.embedding_index)
behavior_service = BehaviorService(config, xinchao_service.evaluator)
task_service = TaskService(config, xinchao_service.evaluator, bucket_manager.embedding_index)
fact_timeline_store = FactTimelineStore(config)
fact_timeline_service = FactTimelineService(
    config, xinchao_service.evaluator, fact_timeline_store, bucket_manager
)
topic_store = TopicStore(config)
living_memory_store = LivingMemoryStore(config)
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

NURSERY_PROXY_MAX_BODY_BYTES = 64 * 1024
NURSERY_PROXY_MAX_RESPONSE_BYTES = 1024 * 1024


def _nursery_brain_settings() -> tuple[str, str]:
    base_url = os.environ.get(
        "OMBRE_BRAIN_INTERNAL_URL", "http://ombre-brain:8000"
    ).strip().rstrip("/")
    token = os.environ.get("OMBRE_NURSERY_INTERNAL_TOKEN", "").strip()
    parsed = urlsplit(base_url)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
    ):
        raise HTTPException(status_code=503, detail="养育室内部服务地址尚未正确配置。")
    if len(token) < 32:
        raise HTTPException(status_code=503, detail="养育室内部服务认证尚未配置。")
    return base_url, token


def _new_nursery_proxy_client() -> httpx.AsyncClient:
    return httpx.AsyncClient(timeout=125.0, follow_redirects=False)


async def _proxy_nursery_request(request: Request, internal_path: str) -> JSONResponse:
    """Proxy an authenticated user request without opening nursery storage."""

    base_url, token = _nursery_brain_settings()
    declared = request.headers.get("content-length", "").strip()
    if declared:
        try:
            if int(declared) > NURSERY_PROXY_MAX_BODY_BYTES:
                raise HTTPException(status_code=413, detail="请求内容过大。")
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="请求长度无效。") from exc
    body = await request.body() if request.method != "GET" else b""
    if len(body) > NURSERY_PROXY_MAX_BODY_BYTES:
        raise HTTPException(status_code=413, detail="请求内容过大。")
    headers = {
        "x-anima-internal-token": token,
        "accept": "application/json",
    }
    if body:
        headers["content-type"] = "application/json"
    request_id = request.headers.get("x-request-id", "").strip()
    if request_id:
        headers["x-request-id"] = request_id[:200]
    try:
        async with _new_nursery_proxy_client() as client:
            response = await client.request(
                request.method,
                f"{base_url}{internal_path}",
                headers=headers,
                params=request.query_params if request.method == "GET" else None,
                content=body,
            )
    except httpx.HTTPError:
        return JSONResponse(
            {
                "ok": False,
                "error": {
                    "code": "NURSERY_SERVICE_UNAVAILABLE",
                    "message": "养育室服务暂时无法连接，已保存的数据没有改变。",
                },
            },
            status_code=503,
        )
    if len(response.content) > NURSERY_PROXY_MAX_RESPONSE_BYTES:
        return JSONResponse(
            {
                "ok": False,
                "error": {
                    "code": "NURSERY_RESPONSE_TOO_LARGE",
                    "message": "养育室服务返回异常，已保存的数据没有改变。",
                },
            },
            status_code=502,
        )
    try:
        payload = response.json()
    except ValueError:
        payload = {
            "ok": False,
            "error": {
                "code": "NURSERY_INVALID_RESPONSE",
                "message": "养育室服务返回了无效结果，已保存的数据没有改变。",
            },
        }
        return JSONResponse(payload, status_code=502)
    return JSONResponse(payload, status_code=response.status_code)


async def _post_nursery_lifecycle(
    internal_path: str, payload: dict
) -> str:
    """Best-effort private lifecycle call; never affects Anima's write path."""

    try:
        base_url, token = _nursery_brain_settings()
        async with httpx.AsyncClient(timeout=5.0, follow_redirects=False) as client:
            response = await client.post(
                f"{base_url}{internal_path}",
                headers={
                    "x-anima-internal-token": token,
                    "accept": "application/json",
                    "content-type": "application/json",
                },
                json=payload,
            )
        try:
            response_payload = response.json()
        except ValueError:
            response_payload = {}
        error = response_payload.get("error", {}) if isinstance(response_payload, dict) else {}
        code = str(error.get("code") or "").strip()
        if 200 <= response.status_code < 300 and response_payload.get("ok") is True:
            return ""
        if code != "SOURCE_NOT_FOUND":
            logger.warning(
                "Nursery lifecycle %s failed: %s",
                internal_path,
                code or f"HTTP {response.status_code}",
            )
        return code
    except Exception as error:
        logger.warning("Nursery lifecycle %s unavailable: %s", internal_path, error)
        return ""


def _anima_nursery_reference(value: object, expected_source_key: str) -> dict | None:
    """Keep only opaque provenance from Xinchao's dedicated safe export."""

    if not isinstance(value, dict):
        return None
    source_key = str(value.get("source_key") or "").strip()
    source_version = str(value.get("source_version") or "").strip()
    if source_key != str(expected_source_key).strip() or not source_version:
        return None
    return {"source_key": source_key, "source_version": source_version}


async def _anima_nursery_references(source_key: str) -> list[dict]:
    """Read Xinchao's opaque safe provenance, never a source body."""

    try:
        references = await asyncio.to_thread(
            xinchao_service.child_safe_source_references_for_nursery, source_key
        )
    except Exception as error:
        logger.warning("Mailbox nursery provenance lookup failed: %s", error)
        return []
    return [
        reference
        for value in references
        if (reference := _anima_nursery_reference(value, source_key)) is not None
    ]


async def _mailbox_nursery_references(message_id: int) -> list[dict]:
    return await _anima_nursery_references(f"mailbox:{int(message_id)}")


async def _memory_nursery_references(bucket_id: str) -> list[dict]:
    return await _anima_nursery_references(f"memory:{str(bucket_id).strip()}")


async def _bridge_anima_nursery_event_once(
    xinchao_result: dict,
    *,
    source_key: str,
    previous_references: list[dict] | None = None,
) -> None:
    """Mirror only a completed, child-safe Anima event into nursery."""

    if not isinstance(xinchao_result, dict) or xinchao_result.get("status") != "applied":
        return
    try:
        event_id = int(xinchao_result.get("event_id") or 0)
        created_at = str(xinchao_result.get("created_at") or "").strip()
        replacement_version = f"{created_at}#{event_id}"[:80]
        if event_id <= 0 or not created_at:
            logger.warning("Completed Anima Xinchao event has no safe provenance")
            return
        safe_event = await asyncio.to_thread(
            xinchao_service.child_safe_event_for_nursery, event_id
        )
    except Exception as error:
        logger.warning("Anima nursery event export failed: %s", error)
        return

    event = {
        key: safe_event.get(key)
        for key in (
            "source_key",
            "source_version",
            "category",
            "child_safe_summary",
            "occurred_at",
        )
    } if isinstance(safe_event, dict) else None
    if event is not None and _anima_nursery_reference(event, source_key) is None:
        logger.warning("Anima nursery event export was not a safe source envelope")
        event = None

    for reference in previous_references or []:
        previous = _anima_nursery_reference(reference, source_key)
        if previous is None:
            continue
        if event is None:
            await _post_nursery_lifecycle(
                "/internal/nursery/anima/family-events/revoke", previous
            )
        else:
            await _post_nursery_lifecycle(
                "/internal/nursery/anima/family-events/supersede",
                {
                    **previous,
                    "superseded_by_source_version": replacement_version,
                },
            )
    if event is None:
        return
    await _post_nursery_lifecycle(
        "/internal/nursery/anima/family-events", {"event": event}
    )


async def _bridge_anima_nursery_event(
    xinchao_result: dict,
    *,
    source_key: str,
    previous_references: list[dict] | None = None,
) -> None:
    """Contain all optional nursery failures outside Anima's primary path."""

    try:
        await _bridge_anima_nursery_event_once(
            xinchao_result,
            source_key=source_key,
            previous_references=previous_references,
        )
    except Exception as error:
        logger.warning("Anima nursery lifecycle bridge failed: %s", error)


async def _revoke_anima_nursery_events(source_key: str) -> None:
    """Best-effort revoke of every exported safe source version."""

    try:
        for reference in await _anima_nursery_references(source_key):
            await _post_nursery_lifecycle(
                "/internal/nursery/anima/family-events/revoke", reference
            )
    except Exception as error:
        logger.warning("Anima nursery revoke bridge failed: %s", error)


async def _revoke_mailbox_nursery_events(message_id: int) -> None:
    await _revoke_anima_nursery_events(f"mailbox:{int(message_id)}")


async def _revoke_memory_nursery_events(bucket_id: str) -> None:
    await _revoke_anima_nursery_events(f"memory:{str(bucket_id).strip()}")


async def _revoke_expired_mailbox_nursery_events(message_ids: list[int]) -> None:
    try:
        for message_id in message_ids:
            await _revoke_mailbox_nursery_events(message_id)
    except Exception as error:
        logger.warning("Expired mailbox nursery revoke bridge failed: %s", error)


mailbox_store.set_expiry_listener(_revoke_expired_mailbox_nursery_events)


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


class ManagerPasswordChange(BaseModel):
    current_password: str = Field(min_length=1, max_length=256)
    new_password: str = Field(min_length=8, max_length=256)
    confirm_password: str = Field(min_length=8, max_length=256)


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
manager_auth_path = data_root / ".clio-manager-auth.json"


def _read_manager_auth() -> dict:
    try:
        payload = json.loads(manager_auth_path.read_text(encoding="utf-8"))
        if payload.get("salt") and payload.get("password_hash") and payload.get("session_secret"):
            return payload
    except (FileNotFoundError, OSError, ValueError, TypeError):
        pass
    return {}


def _password_hash(password: str, salt: str) -> str:
    return hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), bytes.fromhex(salt), 260_000
    ).hex()


def _password_configured() -> bool:
    return bool(_read_manager_auth() or manager_password)


def _password_matches(password: str) -> bool:
    auth = _read_manager_auth()
    if auth:
        supplied = _password_hash(password, str(auth["salt"]))
        return hmac.compare_digest(supplied, str(auth["password_hash"]))
    return bool(manager_password and hmac.compare_digest(password, manager_password))


def _save_manager_password(password: str) -> dict:
    salt = secrets.token_hex(16)
    payload = {
        "version": 1,
        "salt": salt,
        "password_hash": _password_hash(password, salt),
        "session_secret": secrets.token_hex(32),
        "updated_at": now_iso(),
    }
    manager_auth_path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", dir=manager_auth_path.parent, delete=False
    ) as handle:
        json.dump(payload, handle, ensure_ascii=False)
        temporary_path = Path(handle.name)
    os.replace(temporary_path, manager_auth_path)
    try:
        os.chmod(manager_auth_path, 0o600)
    except OSError:
        pass
    return payload


def _manager_session_token() -> str:
    auth = _read_manager_auth()
    secret = str(auth.get("session_secret") or manager_password)
    if not secret:
        return ""
    return hmac.new(
        secret.encode("utf-8"),
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
        if not _password_configured():
            return JSONResponse(
                {"detail": "管理页尚未配置登录密码。"}, status_code=503
            )
        if not _manager_authenticated(request):
            return JSONResponse({"detail": "请先登录管理页。"}, status_code=401)
    response = await call_next(request)
    if (
        request.method in {"POST", "PUT", "PATCH", "DELETE"}
        and path.startswith("/api/")
        and not path.startswith("/api/auth/")
        and path != "/api/behavior/acknowledge"
        and response.status_code < 400
    ):
        try:
            observed = await xinchao_service.observe_presence(
                session_id="manager",
                source=f"manager:{request.method.lower()}:{path}",
                event_id=request.headers.get("x-request-id", ""),
                interrupt_silence=True,
            )
            await behavior_service.store.cancel_for_activity(
                int(observed.get("previous_cycle_id", observed.get("cycle_id", 0)) or 0)
            )
        except Exception as error:
            logger.warning("Manager activity interruption failed: %s", error)
    if path in {"/", "/index.html", "/manage", "/manage/", "/manage/index.html"}:
        response.headers["Cache-Control"] = "no-store, max-age=0"
        response.headers["Pragma"] = "no-cache"
    return response


@app.get("/api/auth/status")
async def manager_auth_status(request: Request) -> dict:
    return {
        "configured": _password_configured(),
        "authenticated": _manager_authenticated(request),
    }


@app.post("/api/auth/login")
async def manager_login(request: Request, payload: ManagerLogin):
    if not _password_configured():
        raise HTTPException(status_code=503, detail="管理页尚未配置登录密码。")
    client_ip = _login_client_ip(request)
    now = time.monotonic()
    attempts = [item for item in login_failures.get(client_ip, []) if now - item < 900]
    if len(attempts) >= 5:
        raise HTTPException(status_code=429, detail="尝试次数过多，请十五分钟后再试。")
    if not _password_matches(payload.password):
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


@app.post("/api/auth/change-password")
async def manager_change_password(request: Request, payload: ManagerPasswordChange):
    if not _manager_authenticated(request):
        raise HTTPException(status_code=401, detail="请先登录管理页。")
    if not _password_matches(payload.current_password):
        raise HTTPException(status_code=400, detail="当前密码不正确。")
    if payload.new_password != payload.confirm_password:
        raise HTTPException(status_code=400, detail="两次输入的新密码不一致。")
    _save_manager_password(payload.new_password)
    response = JSONResponse({"ok": True, "updated_at": now_iso()})
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


async def _record_xinchao(
    content: str,
    source_tool: str,
    source_ref: str,
    *,
    correction_key: str = "",
) -> dict:
    if source_tool not in {
        "mailbox",
        "manager_memory",
        "manager_append",
        "manager_create",
    }:
        logger.info("Skipped non-affective manager source: %s", source_tool)
        return {"status": "display_only", "source_tool": source_tool}
    try:
        result = await xinchao_service.record_event(
            content,
            source_tool,
            source_ref,
            correction_key=correction_key,
        )
        if isinstance(result, dict) and result.get("status") in {"pending", "applied"}:
            state = await xinchao_service.status()
            await behavior_service.store.cancel_for_activity(state.get("cycle_id", 0))
            superseded = result.get("superseded") or {}
            if superseded.get("previous_cycle_id") is not None:
                await behavior_service.store.cancel_for_activity(
                    int(superseded.get("previous_cycle_id") or 0)
                )
        if result.get("status") == "applied":
            correction = result.get("correction") or {}
            if correction.get("supersedes_event_id"):
                await behavior_service.store.cancel_source_event(
                    int(correction["supersedes_event_id"])
                )
            await behavior_service.schedule_event(result, state)
        return result
    except Exception as error:
        logger.warning("Manager Xinchao hook failed after successful write: %s", error)
        return {"status": "pending", "error": str(error)}


async def _record_sidecars(
    content: str,
    source_tool: str,
    source_ref: str,
    *,
    correction_key: str = "",
    previous_mailbox_references: list[dict] | None = None,
    previous_memory_references: list[dict] | None = None,
) -> None:
    event_key = hashlib.sha256(
        f"{source_tool}\0{source_ref}\0{' '.join(content.split())}".encode("utf-8")
    ).hexdigest()
    xinchao_result = await _record_xinchao(
        content, source_tool, source_ref, correction_key=correction_key
    )
    correction = xinchao_result.get("correction") or {}
    if correction.get("supersedes_event_id"):
        try:
            await behavior_service.store.cancel_source_event(
                int(correction["supersedes_event_id"])
            )
            await task_service.retract_source(source_tool, source_ref)
            await fact_timeline_service.retract_source(source_tool, source_ref)
        except Exception as error:
            logger.warning("Manager correction rollback was partial: %s", error)
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
    if source_tool == "mailbox":
        await _bridge_anima_nursery_event(
            xinchao_result,
            source_key=f"mailbox:{int(source_ref)}",
            previous_references=previous_mailbox_references,
        )
    elif source_tool in {"manager_memory", "manager_append", "manager_create"}:
        await _bridge_anima_nursery_event(
            xinchao_result,
            source_key=f"memory:{source_ref}",
            previous_references=previous_memory_references,
        )


async def _xinchao_memory_resonance_provider(
    state: dict, event_contexts: list[dict]
) -> list[dict]:
    settings = config.get("xinchao", {})
    if not bool(settings.get("memory_resonance_enabled", True)):
        return []
    max_items = max(1, min(4, int(settings.get("memory_resonance_max_items", 3))))
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
    state_text = " ".join(name for name, value in strongest if float(value) >= 0.25)
    query = " ".join(part for part in (context_text, state_text) if part).strip()
    if not query:
        return []
    result: list[dict] = []
    try:
        for bucket in await bucket_manager.search(
            query,
            limit=max_items,
            use_semantic=True,
            include_sealed=False,
            semantic_min_similarity=threshold,
            record_feedback=False,
        ):
            metadata = bucket.get("metadata") or {}
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
        logger.warning("Manager memory resonance search unavailable: %s", error)
    try:
        for item in await search_mailbox(
            mailbox_store,
            bucket_manager.embedding_index,
            query,
            limit=max_items,
            include_deleted=False,
        ):
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
        logger.warning("Manager mailbox resonance search unavailable: %s", error)
    result.sort(key=lambda item: float(item.get("relevance", 0.0)), reverse=True)
    return result[:max_items]


async def _xinchao_task_context_provider(
    state: dict, event_contexts: list[dict]
) -> list[dict]:
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
            "details": str(item.get("details", ""))[:500],
            "importance": item["importance"],
        }
        for item in items
    ]


xinchao_service.set_memory_resonance_provider(_xinchao_memory_resonance_provider)
# 未竟、时间线和小金库只用于管理与展示，不进入情绪判断。
# 暗涌与推送只读取当前状态，不再把输出反向写回48项状态。
behavior_service.set_tendency_provider(xinchao_service.behavior_tendency_context)


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


class TopicCreate(BaseModel):
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
    status: Literal["planned", "in_progress", "waiting", "completed"] | None = None


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


class RelationshipOverrideRequest(BaseModel):
    value: float = Field(ge=0, le=200)
    reason: str = Field(default="", max_length=240)


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


@app.get("/api/nursery/status")
async def nursery_status(request: Request) -> JSONResponse:
    return await _proxy_nursery_request(request, "/internal/nursery/user/status")


@app.get("/api/nursery/entry-preference")
@app.put("/api/nursery/entry-preference")
async def nursery_entry_preference(request: Request) -> JSONResponse:
    return await _proxy_nursery_request(
        request, "/internal/nursery/user/entry-preference"
    )


@app.post("/api/nursery/drafts")
async def nursery_start_draft(request: Request) -> JSONResponse:
    return await _proxy_nursery_request(request, "/internal/nursery/user/drafts")


@app.get("/api/nursery/drafts/{child_id}")
async def nursery_creation_draft(request: Request, child_id: str) -> JSONResponse:
    target = quote(str(child_id).strip(), safe="")
    return await _proxy_nursery_request(
        request, f"/internal/nursery/user/drafts/{target}"
    )


@app.post("/api/nursery/drafts/{child_id}/operations")
async def nursery_creation_operation(request: Request, child_id: str) -> JSONResponse:
    target = quote(str(child_id).strip(), safe="")
    return await _proxy_nursery_request(
        request, f"/internal/nursery/user/drafts/{target}/operations"
    )


@app.get("/api/nursery/operations/{operation_id}")
async def nursery_operation_result(request: Request, operation_id: str) -> JSONResponse:
    target = quote(str(operation_id).strip(), safe="")
    return await _proxy_nursery_request(
        request, f"/internal/nursery/user/operations/{target}"
    )


@app.post("/api/nursery/children/{child_id}/interactions")
async def nursery_child_interaction(request: Request, child_id: str) -> JSONResponse:
    target = quote(str(child_id).strip(), safe="")
    return await _proxy_nursery_request(
        request, f"/internal/nursery/user/children/{target}/interactions"
    )


@app.get("/api/nursery/children/{child_id}/status")
async def nursery_child_status(request: Request, child_id: str) -> JSONResponse:
    target = quote(str(child_id).strip(), safe="")
    return await _proxy_nursery_request(
        request, f"/internal/nursery/user/children/{target}/status"
    )


@app.post("/api/nursery/children/{child_id}/operations")
async def nursery_child_operation(request: Request, child_id: str) -> JSONResponse:
    target = quote(str(child_id).strip(), safe="")
    return await _proxy_nursery_request(
        request, f"/internal/nursery/user/children/{target}/operations"
    )


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
    await _record_sidecars(
        payload.content.strip(),
        "manager_memory",
        bucket_id,
        correction_key=f"memory:{bucket_id}",
    )
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
    previous_memory_references = None
    if payload.content is not None and "content" in updates and not xinchao_content:
        previous_memory_references = await _memory_nursery_references(bucket_id)
    if xinchao_content:
        await _record_sidecars(xinchao_content, "manager_append", bucket_id)
    elif payload.content is not None and "content" in updates:
        await _record_sidecars(
            str(updates["content"]),
            "manager_memory",
            bucket_id,
            correction_key=f"memory:{bucket_id}",
            previous_memory_references=previous_memory_references,
        )
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
    await _revoke_memory_nursery_events(bucket_id)
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
    await _revoke_memory_nursery_events(bucket_id)
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
    for branch in topic_store.tree():
        main = branch["main_topic"]
        subtopics = branch["subtopics"]
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


@app.post("/api/topics")
async def create_topic(payload: TopicCreate) -> dict:
    try:
        item = await topic_store.add_topic(payload.main_topic, payload.subtopic)
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    return {"ok": True, "item": item}


@app.delete("/api/topics")
async def delete_topic(main_topic: str, subtopic: str) -> dict:
    try:
        removed = await topic_store.remove_topic(main_topic, subtopic)
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    return {"ok": True, "removed": removed}


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
            topic_store.validate(main_topic, subtopic)
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
        tree = {
            item["main_topic"]: list(item["subtopics"])
            for item in topic_store.tree()
        }
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
                        main, sub = topic_store.validate(
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
    q: str = Query(default="", max_length=300),
    source: Literal["all", "memory", "mailbox", "thoughts"] = "all",
    person: str = Query(default="", max_length=120),
    date: str = Query(default="", max_length=10),
    limit: int = Query(default=12, ge=1, le=100),
) -> dict:
    """Read-only hybrid search by text, person and/or Beijing calendar date."""
    query = q.strip()
    person_filter = person.strip()
    date_filter = date.strip()
    if date_filter and not re.fullmatch(r"\d{4}-\d{2}-\d{2}", date_filter):
        raise HTTPException(status_code=400, detail="日期必须使用 YYYY-MM-DD。")
    if not (query or person_filter or date_filter):
        raise HTTPException(status_code=400, detail="请至少填写关键字、人物或日期中的一项。")

    def matches_filters(item: dict) -> bool:
        searchable = json.dumps(item, ensure_ascii=False, default=str)
        if person_filter and person_filter.casefold() not in searchable.casefold():
            return False
        if date_filter and date_filter not in searchable:
            return False
        return True

    memory_items = []
    mailbox_items = []
    thought_items = []
    if source in {"all", "memory"}:
        if query:
            matches = await bucket_manager.search(
                query,
                limit=max(limit * 3, 30),
                use_semantic=True,
                include_sealed=False,
                record_feedback=False,
            )
        else:
            matches = await bucket_manager.list_all(
                include_archive=True, include_sealed=False
            )
        for bucket in matches:
            view = _bucket_view(bucket)
            matched = bucket.get("matched_segment") or {}
            snippet = str(matched.get("content") or view["summary"])
            item = {
                **view,
                "source": "memory",
                "snippet": _summary(snippet, 240),
                "score": bucket.get("score"),
                "semantic_score": bucket.get("semantic_score"),
            }
            if matches_filters(item):
                memory_items.append(item)
    if source in {"all", "mailbox"}:
        if query:
            matches = await search_mailbox(
                mailbox_store,
                bucket_manager.embedding_index,
                query,
                limit=max(limit * 3, 30),
            )
        else:
            matches = await mailbox_store.search_pool(
                include_deleted=False, limit=max(limit * 10, 100)
            )
        mailbox_items = []
        for item in matches:
            public_item = {
                **item,
                "source": "mailbox",
                "title": "窗口交接信",
                "snippet": _summary(item.get("message", ""), 240),
                "score": item.get("match_score"),
            }
            if matches_filters(public_item):
                mailbox_items.append(public_item)
    if source in {"all", "thoughts"}:
        if query:
            matches = await xinchao_service.search_private_thoughts(
                query,
                kind="all",
                limit=max(limit * 3, 30),
            )
        else:
            matches = await xinchao_service.list_private_thoughts(
                status="all", limit=max(limit * 10, 100)
            )
        thought_items = []
        for item in matches:
            public_item = {
                **item,
                "source": item.get("source", "thought"),
                "title": item.get("kind_label", "心念"),
                "snippet": _summary(item.get("thought_text", ""), 240),
                "score": round(float(item.get("match_score", 0.0)) * 100, 2),
                "semantic_score": item.get("semantic_score"),
                "private": True,
                "read_only": True,
            }
            if matches_filters(public_item):
                thought_items.append(public_item)
    items = sorted(
        [*memory_items, *mailbox_items, *thought_items],
        key=lambda item: float(item.get("score") or 0),
        reverse=True,
    )[:limit]
    return {
        "query": query,
        "source": source,
        "person": person_filter,
        "date": date_filter,
        "items": items,
        "count": len(items),
    }


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
            # A legacy or partially migrated bucket may have a NULL metadata
            # value. One malformed source must not take down the whole timeline.
            metadata = (bucket or {}).get("metadata") or {}
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
        result = await fact_timeline_service.confirm_candidate(candidate_id)
        return result
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
    previous_references = await _mailbox_nursery_references(message_id)
    await _record_sidecars(
        payload.message,
        "mailbox",
        str(message_id),
        correction_key=f"mailbox:{message_id}",
        previous_mailbox_references=previous_references,
    )
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
    await _revoke_mailbox_nursery_events(message_id)
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


@app.get("/api/xinchao/relationship")
async def xinchao_relationship() -> dict:
    """Read the explainable 0-200 current relationship climate."""
    return await xinchao_service.relationship_status()


@app.put("/api/xinchao/relationship")
async def set_xinchao_relationship(payload: RelationshipOverrideRequest) -> dict:
    """Temporarily calibrate the current moment without rewriting history."""
    return await xinchao_service.set_relationship_override(
        payload.value, payload.reason
    )


@app.post("/api/xinchao/relationship/restore")
async def restore_xinchao_relationship() -> dict:
    """Release a current-only calibration and return to automatic judgement."""
    return await xinchao_service.restore_relationship_auto()


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
    status: Literal["active", "all", "flash", "obsession", "resolved", "faded"] = "active",
    limit: int = Query(default=100, ge=1, le=500),
) -> dict:
    """Private inner thoughts never enter Bark, mailbox, or factual buckets."""
    items = await xinchao_service.list_private_thoughts(
        status=status, limit=limit, kind="inner"
    )
    return {
        "items": items,
        "count": len(items),
        "privacy": "inner_only",
        "outward_delivery": False,
    }


@app.get("/api/mind/traces")
async def private_thought_traces(
    q: str = Query(default="", max_length=300),
    date: str = Query(default="", max_length=10),
    limit: int = Query(default=100, ge=1, le=500),
) -> dict:
    """Read-only timeline/search view for AI-written private thought traces."""
    items = await xinchao_service.search_private_thoughts(
        q,
        kind="trace",
        date=date,
        limit=limit,
    )
    return {
        "items": items,
        "count": len(items),
        "kind": "trace",
        "display_name": "念痕",
        "privacy": "inner_only",
        "read_only": True,
        "ai_write_only": True,
        "search": q.strip(),
        "date": date.strip(),
    }


@app.post("/api/mind/thoughts/{canonical_tag}/resolve")
async def resolve_private_thought(canonical_tag: str) -> dict:
    if await xinchao_service.is_read_only_trace(canonical_tag):
        raise HTTPException(status_code=403, detail="念痕只能由 AI 写入，网页只读，不能修改。")
    if not await xinchao_service.resolve_private_thought(canonical_tag):
        raise HTTPException(status_code=404, detail="没有找到这条心念。")
    return {"ok": True, "canonical_tag": canonical_tag, "status": "resolved"}


@app.delete("/api/mind/thoughts/{canonical_tag}")
async def delete_private_thought(canonical_tag: str) -> dict:
    if await xinchao_service.is_read_only_trace(canonical_tag):
        raise HTTPException(status_code=403, detail="念痕只能由 AI 写入，网页只读，不能删除。")
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
    recent_links = await xinchao_service.recent_linkages(limit=30)
    for link in recent_links:
        echo = link.get("memory_echo") or {}
        if not echo:
            continue
        items.append(
            {
                **echo,
                "score": round(float(echo.get("relevance") or 0.0), 4),
                "why": "这次写入唤起了相似记忆，形成一次较轻的记忆回响",
                "event_id": link.get("event_id"),
                "created_at": link.get("created_at"),
                "trigger": link.get("summary") or link.get("evidence") or "一次写入",
            }
        )
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
        if not any(
            str(existing.get("source_id") or "")
            == str(item.get("bucket_id") or item.get("message_id") or "")
            for existing in items
        ):
            items.append(item)
    tension = await xinchao_tension()
    return {
        "name": "内在牵引",
        "item_label": "记忆回响",
        "items": items[:12],
        "links": items[:12],
        "count": len(items),
        "as_of": state.get("as_of"),
        "tension": tension,
        "strongest": tension.get("strongest") or {},
        "counterweight": tension.get("counterweight") or {},
        "balance": tension.get("balance", 0.0),
    }


@app.get("/api/xinchao/transitions")
async def xinchao_transitions(
    limit: int = Query(default=50, ge=1, le=200),
) -> dict:
    items = await xinchao_service.recent_transitions(limit=limit)
    return {"items": items, "count": len(items)}


@app.get("/api/xinchao/linkages")
async def xinchao_linkages(
    limit: int = Query(default=30, ge=1, le=200),
) -> dict:
    """Readable write -> hormone links, including historical events."""
    items = await xinchao_service.recent_linkages(limit=limit)
    return {"items": items, "count": len(items)}


@app.get("/api/disposition")
async def disposition(days: int = Query(default=30, ge=7, le=120)) -> dict:
    return await xinchao_service.disposition_preview(days=days)


@app.get("/api/brain/context")
async def brain_context(
    q: str = Query(default="", max_length=500),
    limit: int = Query(default=6, ge=1, le=20),
) -> dict:
    """Return the same cross-layer read model exposed to the using AI."""
    return await build_brain_context(
        q,
        bucket_manager=bucket_manager,
        mailbox_store=mailbox_store,
        xinchao_service=xinchao_service,
        task_service=task_service,
        fact_timeline_store=fact_timeline_store,
        limit=limit,
    )


@app.get("/api/living-memory/{bucket_id}")
async def living_memory(bucket_id: str) -> dict:
    bucket = await bucket_manager.get(bucket_id)
    if not bucket:
        raise HTTPException(status_code=404, detail="没有找到这条记忆。")
    metadata = bucket.get("metadata") or {}
    if bool(metadata.get("sealed")) or str(metadata.get("type", "")).lower() == "archived":
        raise HTTPException(status_code=403, detail="封存记忆不重新计算单条认知脉络。")
    relations, facts, topic = await asyncio.gather(
        relation_store.related_details(bucket_id),
        fact_timeline_store.versions_for_bucket(bucket_id),
        topic_store.get(bucket_id),
    )
    coordinates = living_memory_store.build(
        bucket,
        relations=relations,
        facts=facts,
        topic=topic,
    )
    return await living_memory_store.save(coordinates)


@app.get("/api/home")
async def home_state() -> dict:
    (
        current,
        tension,
        darkflow,
        traces,
        thoughts,
        disposition_value,
        transitions,
        boot,
        phrase,
        pending,
    ) = await asyncio.gather(
        xinchao_service.status(),
        xinchao_tension(),
        xinchao_service.darkflow_status(),
        xinchao_service.search_private_thoughts("", kind="trace", limit=20),
        xinchao_service.list_private_thoughts(status="active", limit=20),
        xinchao_service.disposition_preview(days=30),
        xinchao_service.recent_transitions(limit=30),
        xinchao_service.latest_boot_delivery(),
        _get_house_phrase(),
        behavior_service.store.pending_handoff_summary(),
    )
    last_delivery_at = str((boot or {}).get("delivered_at") or "")
    current_trace = next(
        (
            item
            for item in traces
            if not item.get("resolved")
            and (
                not last_delivery_at
                or str(item.get("last_seen") or item.get("created_at") or "")
                >= last_delivery_at
            )
        ),
        None,
    )
    if current_trace:
        most_wanted = {"source": "trace", "display_name": "念痕", "item": current_trace}
    elif darkflow and darkflow.get("content"):
        most_wanted = {"source": "darkflow", "display_name": "暗涌", "item": darkflow}
    elif thoughts:
        most_wanted = {"source": "thought", "display_name": "心念", "item": thoughts[0]}
    else:
        most_wanted = {"source": "", "display_name": "", "item": None}
    return {
        "as_of": current.get("as_of") or now_iso(),
        "state": current,
        "tension": tension,
        "most_wanted": most_wanted,
        "disposition": disposition_value,
        "transitions": transitions,
        "latest_boot_delivery": boot,
        "house_phrase": {
            "text": phrase.get("text") or HOUSE_PHRASE_FALLBACK,
            "generated_at": phrase.get("generated_at", ""),
        },
        "pending_push": pending,
    }


@app.get("/api/continuity/status")
async def continuity_status() -> dict:
    state, boot, darkflow, mailbox, pending = await asyncio.gather(
        xinchao_service.status(),
        xinchao_service.latest_boot_delivery(),
        xinchao_service.darkflow_status(),
        mailbox_store.list(limit=1, include_deleted=False),
        behavior_service.store.pending_handoff_summary(),
    )
    return {
        "as_of": now_iso(),
        "state": state,
        "latest_boot_delivery": boot,
        "darkflow": darkflow,
        "latest_mailbox": mailbox[0] if mailbox else None,
        "pending_push": pending,
    }


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
            {"id": "tasks", "name": "未竟", "description": "管理还没有完成的事", "icon": "circle-check-big"},
            {"id": "treasury", "name": "小金库", "description": "AI自己的收入与支出", "icon": "wallet-cards"},
            {"id": "mailbox", "name": "信箱", "description": "窗口之间留下的接力信", "icon": "mail-open"},
            {"id": "darkflow", "name": "暗涌", "description": "沉默期间形成的一封内心沉淀", "icon": "waves"},
            {"id": "thoughts", "name": "念痕", "description": "当前 AI 留下的真实当下", "icon": "feather"},
            {"id": "mind", "name": "心念", "description": "沉默中浮现的闪念与执念", "icon": "sparkles"},
            {"id": "resonance", "name": "内在牵引", "description": "查看一次写入怎样唤起较轻的记忆回响", "icon": "radio"},
            {"id": "behavior", "name": "行为与推送", "description": "查看推送判断和送达状态", "icon": "send"},
            {"id": "personality", "name": "性格轨迹", "description": "查看本月倾向怎样形成", "icon": "route"},
            {"id": "coordinates", "name": "认知脉络", "description": "汇合时间、关系、事实、情绪与沉淀", "icon": "network"},
            {"id": "settings", "name": "设置与安全", "description": "管理密码、推送名称和判定规则", "icon": "settings"},
        ]
    }


@app.get("/api/behavior/actions")
async def behavior_actions(
    limit: int = Query(default=30, ge=1, le=100),
    before_id: int = Query(default=0, ge=0),
) -> dict:
    items = await behavior_service.store.list(limit=limit, before_id=before_id)
    candidates = await behavior_service.store.list_candidates(limit=limit)
    status_labels = {
        "sent": "已推送", "skipped": "本次未推送", "cancelled": "已取消",
        "expired": "已结束", "pending": "等待判断", "waiting": "等待合适时机",
        "held": "暂存", "rehearsal": "生成中",
    }
    for item in items:
        context = item.get("context") or {}
        item["status_label"] = status_labels.get(str(item.get("status") or ""), "推送记录")
        item["display_content"] = str(item.get("content") or "").strip()
        item["display_reason"] = str(
            context.get("reason") or item.get("error") or ""
        ).strip()
        item["trigger_summary"] = str(context.get("trigger_summary") or "").strip()
        item["emotion_drivers"] = list(context.get("emotion_drivers") or [])
        item["record_kind"] = "delivery"
    for item in candidates:
        context = item.get("decision_context") or {}
        item["status_label"] = status_labels.get(str(item.get("status") or ""), "推送判断")
        item["display_reason"] = str(item.get("decision_note") or "").strip()
        item["trigger_summary"] = str(context.get("trigger_summary") or "").strip()
        item["emotion_drivers"] = list(context.get("emotion_drivers") or [])
        item["next_review_at"] = item.get("due_at") if item.get("status") == "waiting" else None
        item["record_kind"] = "decision"
    state = await xinchao_service.status()
    latest_candidate = candidates[0] if candidates else {}
    latest_context = latest_candidate.get("decision_context") or {}
    latest_action = next(
        (
            item for item in items
            if int(item.get("cycle_id") or -1) == int(state.get("cycle_id") or -2)
        ),
        {},
    )
    event_contexts = list(state.get("event_contexts") or [])
    trigger_summary = str(
        latest_context.get("trigger_summary")
        or ((event_contexts[-1] if event_contexts else {}).get("context_card"))
        or state.get("event_summary")
        or "本轮没有单独的文字写入"
    ).strip()
    drivers = list(latest_context.get("emotion_drivers") or [])
    if not drivers:
        ranked = []
        for name, raw_value in (state.get("pipes") or {}).items():
            try:
                ranked.append({"name": str(name), "value": round(float(raw_value), 4)})
            except (TypeError, ValueError):
                continue
        drivers = sorted(ranked, key=lambda row: -row["value"])[:6]
    phase = str(state.get("interaction_phase") or "closed")
    if phase == "active":
        phase_label = "30分钟候静默计时中"
        phase_detail = "有任何新操作都会从头计时；这段时间不会生成暗涌，也不会判断推送。"
    elif latest_candidate.get("status") == "waiting":
        phase_label = "DeepSeek决定再等等"
        phase_detail = str(latest_candidate.get("decision_note") or "稍后重新判断是否适合推送")
    elif latest_action:
        phase_label = str(latest_action.get("status_label") or "本轮判断完成")
        phase_detail = str(
            latest_action.get("display_reason")
            or latest_action.get("display_content")
            or "本轮判断已经完成"
        )
    elif latest_candidate:
        phase_label = str(latest_candidate.get("status_label") or "正在判断")
        phase_detail = str(latest_candidate.get("decision_note") or "正在判断是否需要推送")
    elif phase == "absence":
        phase_label = "已进入静默，等待判断"
        phase_detail = "候静默已经完整结束，正在结合本轮写入和48项情绪进行判断。"
    else:
        phase_label = "当前没有进行中的推送判断"
        phase_detail = "下一次真实写入或操作后会开始新的30分钟候静默。"
    trajectory = {
        "cycle_id": state.get("cycle_id"),
        "phase": phase,
        "phase_label": phase_label,
        "phase_detail": phase_detail,
        "last_activity_at": state.get("last_presence_at") or state.get("last_event_at"),
        "silence_due_at": (state.get("timing") or {}).get("absence_due_at"),
        "silence_started_at": state.get("absence_started_at"),
        "next_review_at": latest_candidate.get("next_review_at"),
        "trigger_summary": trigger_summary,
        "emotion_drivers": drivers,
        "pipe_count": len(state.get("pipes") or {}),
    }
    pending = await behavior_service.store.pending_handoff_summary()
    return {
        "items": items,
        "sent_items": [item for item in items if item.get("status") == "sent"],
        "decisions": candidates,
        "candidates": candidates,
        "count": await asyncio.to_thread(behavior_service.store.count),
        "mode": behavior_service.mode,
        "configured": behavior_service.configured,
        "push_title": await behavior_service.push_title(),
        "pending": pending,
        "trajectory": trajectory,
    }


@app.get("/api/behavior/settings")
async def behavior_settings() -> dict:
    return {
        "push_title": await behavior_service.push_title(),
        "mode": behavior_service.mode,
        "configured": behavior_service.configured,
    }


@app.put("/api/behavior/settings")
async def update_behavior_settings(payload: BehaviorSettingsUpdate) -> dict:
    try:
        title = await behavior_service.set_push_title(payload.push_title)
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    return {
        "ok": True,
        "push_title": title,
        "mode": behavior_service.mode,
        "configured": behavior_service.configured,
    }


@app.get("/api/behavior/pending")
async def behavior_pending() -> dict:
    """Expose acknowledgement state without repeating push plaintext."""
    return await behavior_service.store.pending_handoff_summary()


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
    return {
        "status": "acknowledged",
        "phase": acknowledged.get("phase") or "silence",
        "message": "已确认这条推送；确认本身不算继续聊天，不更新活动时间，不新开周期，也不清除暗涌状态。记录会保留到 AI 成功读完开机摘要后再清理。",
        "acknowledged_at": acknowledged.get("acknowledged_at"),
        "count": acknowledged.get("count", 0),
        "active_started_at": None,
    }


@app.get("/api/xinchao/judge")
async def xinchao_judge() -> dict:
    """Read the editable private judge book without exposing API credentials."""
    evaluator = xinchao_service.evaluator
    return {
        **evaluator.read_judge_config(),
        "base_rules": EVALUATOR_PROMPT.format(
            pipes="、".join(PIPE_NAMES), pipe_guide=PIPE_GUIDE
        ),
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
