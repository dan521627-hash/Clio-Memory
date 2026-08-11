import asyncio
import json
import logging
import os
import time
from collections import OrderedDict
from contextvars import ContextVar
from inspect import isawaitable
from logging.handlers import RotatingFileHandler
from typing import Any

from utils import now_iso


_CURRENT_MCP_SESSION_ID: ContextVar[str] = ContextVar(
    "ombre_current_mcp_session_id", default=""
)
_CURRENT_MCP_REQUEST_ID: ContextVar[str] = ContextVar(
    "ombre_current_mcp_request_id", default=""
)


def current_mcp_session_id() -> str:
    return _CURRENT_MCP_SESSION_ID.get()


def current_mcp_event_id() -> str:
    session_id = _CURRENT_MCP_SESSION_ID.get()
    request_id = _CURRENT_MCP_REQUEST_ID.get()
    if not request_id:
        return ""
    return f"{session_id}\0{request_id}"


class MCPRequestDiagnosticsMiddleware:
    """Write privacy-limited MCP request metadata to a rotating JSONL log."""

    def __init__(
        self,
        app,
        log_path: str | None = None,
        max_bytes: int = 5 * 1024 * 1024,
        backup_count: int = 5,
        max_sessions: int = 4096,
        activity_callback=None,
    ):
        self.app = app
        self.log_path = log_path or os.environ.get(
            "OMBRE_DIAGNOSTIC_LOG", "/data/logs/mcp_requests.log"
        )
        self.max_sessions = max_sessions
        self.activity_callback = activity_callback
        self._session_clients: OrderedDict[str, dict[str, str]] = OrderedDict()
        self._logger = self._build_logger(max_bytes, backup_count)

    def _build_logger(self, max_bytes: int, backup_count: int):
        try:
            os.makedirs(os.path.dirname(os.path.abspath(self.log_path)), exist_ok=True)
            logger = logging.getLogger(f"ombre_brain.mcp_requests.{id(self)}")
            logger.setLevel(logging.INFO)
            logger.propagate = False
            handler = RotatingFileHandler(
                self.log_path,
                maxBytes=max_bytes,
                backupCount=backup_count,
                encoding="utf-8",
            )
            handler.setFormatter(logging.Formatter("%(message)s"))
            logger.addHandler(handler)
            return logger
        except OSError:
            logging.getLogger("ombre_brain").exception(
                "Unable to initialize MCP diagnostic log: %s", self.log_path
            )
            return None

    @staticmethod
    def _headers(scope) -> dict[str, str]:
        return {
            key.decode("latin-1").lower(): value.decode("latin-1")
            for key, value in scope.get("headers", [])
        }

    @staticmethod
    def _safe_client_info(value: Any) -> dict[str, str] | None:
        if not isinstance(value, dict):
            return None
        safe = {}
        for key in ("name", "version", "title"):
            item = value.get(key)
            if isinstance(item, (str, int, float, bool)):
                safe[key] = str(item)[:256]
        return safe or None

    @classmethod
    def _messages(cls, body: bytes, http_method: str) -> list[dict[str, Any]]:
        try:
            payload = json.loads(body.decode("utf-8")) if body else None
        except (UnicodeDecodeError, json.JSONDecodeError):
            payload = None

        items = payload if isinstance(payload, list) else [payload]
        messages = []
        for item in items:
            if not isinstance(item, dict) or not isinstance(item.get("method"), str):
                continue
            params = item.get("params")
            client_info = None
            tool_name = None
            if item["method"] == "initialize" and isinstance(params, dict):
                client_info = cls._safe_client_info(params.get("clientInfo"))
            elif item["method"] == "tools/call" and isinstance(params, dict):
                name = params.get("name")
                if isinstance(name, (str, int, float, bool)):
                    tool_name = str(name)[:256]
            messages.append(
                {
                    "method": item["method"],
                    "client_info": client_info,
                    "tool_name": tool_name,
                    "request_id": str(item.get("id", ""))[:120],
                }
            )

        if messages:
            return messages
        fallback = {
            "GET": "stream/open",
            "DELETE": "session/delete",
            "OPTIONS": "http/options",
        }.get(http_method.upper(), f"http/{http_method.lower()}")
        return [
            {
                "method": fallback,
                "client_info": None,
                "tool_name": None,
                "request_id": "",
            }
        ]

    @staticmethod
    def _is_disconnect_exception(error: BaseException) -> bool:
        if isinstance(
            error,
            (asyncio.CancelledError, BrokenPipeError, ConnectionResetError),
        ):
            return True
        return error.__class__.__name__ in {
            "ClientDisconnect",
            "BrokenResourceError",
            "ClosedResourceError",
            "EndOfStream",
        }

    def _remember_client(self, session_id: str, client_info: dict[str, str] | None):
        if not session_id or not client_info:
            return
        self._session_clients[session_id] = client_info
        self._session_clients.move_to_end(session_id)
        while len(self._session_clients) > self.max_sessions:
            self._session_clients.popitem(last=False)

    def _write(self, record: dict[str, Any]):
        if self._logger is not None:
            self._logger.info(json.dumps(record, ensure_ascii=False, separators=(",", ":")))

    async def __call__(self, scope, receive, send):
        if scope.get("type") != "http" or scope.get("path") != "/mcp":
            await self.app(scope, receive, send)
            return

        started_at = time.perf_counter()
        response_body_bytes = 0
        stream_completed = False
        client_disconnected = False
        captured = []
        body_parts = []
        while True:
            message = await receive()
            captured.append(message)
            if message.get("type") == "http.request":
                body_parts.append(message.get("body", b""))
                if not message.get("more_body", False):
                    break
            else:
                if message.get("type") == "http.disconnect":
                    client_disconnected = True
                break

        replay_index = 0

        async def replay_receive():
            nonlocal replay_index, client_disconnected
            if replay_index < len(captured):
                message = captured[replay_index]
                replay_index += 1
            else:
                message = await receive()
            if message.get("type") == "http.disconnect" and not stream_completed:
                client_disconnected = True
            return message

        request_headers = self._headers(scope)
        request_session = request_headers.get("mcp-session-id", "")
        user_agent = request_headers.get("user-agent", "")[:512]
        messages = self._messages(b"".join(body_parts), scope.get("method", ""))
        response_status = 500
        response_session = ""
        context_token = _CURRENT_MCP_SESSION_ID.set(request_session)
        request_token = _CURRENT_MCP_REQUEST_ID.set(
            next(
                (
                    item.get("request_id", "")
                    for item in messages
                    if item.get("method") == "tools/call"
                ),
                "",
            )
        )

        async def diagnostic_send(message):
            nonlocal response_status, response_session
            nonlocal response_body_bytes, stream_completed, client_disconnected
            if message.get("type") == "http.response.start":
                response_status = int(message.get("status", 0))
                response_headers = {
                    key.decode("latin-1").lower(): value.decode("latin-1")
                    for key, value in message.get("headers", [])
                }
                response_session = response_headers.get("mcp-session-id", "")
            try:
                await send(message)
            except BaseException as error:
                if not stream_completed and self._is_disconnect_exception(error):
                    client_disconnected = True
                raise
            if message.get("type") == "http.response.body":
                response_body_bytes += len(message.get("body", b""))
                if not message.get("more_body", False):
                    stream_completed = True

        try:
            await self.app(scope, replay_receive, diagnostic_send)
        except BaseException as error:
            if not stream_completed and self._is_disconnect_exception(error):
                client_disconnected = True
            raise
        finally:
            duration_ms = round((time.perf_counter() - started_at) * 1000, 3)
            initialize_info = next(
                (item["client_info"] for item in messages if item["method"] == "initialize"),
                None,
            )
            effective_session = request_session or response_session
            if response_session and initialize_info:
                self._remember_client(response_session, initialize_info)
            inherited_info = self._session_clients.get(effective_session)

            for item in messages:
                record = {
                    "timestamp": now_iso(),
                    "method": item["method"],
                    "has_session_header": bool(request_session),
                    "session_id_prefix": effective_session[:6] or None,
                    "session_source": (
                        "request" if request_session else "response" if response_session else None
                    ),
                    "client_info": item["client_info"] or inherited_info,
                    "user_agent": user_agent or None,
                    "status": response_status,
                }
                if item["method"] == "tools/call":
                    record.update(
                        {
                            "tool_name": item["tool_name"],
                            "duration_ms": duration_ms,
                            "response_body_bytes": response_body_bytes,
                            "stream_completed": stream_completed,
                            "client_disconnected": client_disconnected,
                        }
                    )
                self._write(record)

            if self.activity_callback is not None:
                try:
                    callback_result = self.activity_callback(
                        effective_session,
                        [
                            {
                                "method": item["method"],
                                "tool_name": item["tool_name"],
                                "request_id": item.get("request_id", ""),
                            }
                            for item in messages
                        ],
                    )
                    if isawaitable(callback_result):
                        await callback_result
                except Exception:
                    logging.getLogger("ombre_brain").exception(
                        "MCP activity callback failed"
                    )

            if scope.get("method", "").upper() == "DELETE" and request_session:
                self._session_clients.pop(request_session, None)
            _CURRENT_MCP_REQUEST_ID.reset(request_token)
            _CURRENT_MCP_SESSION_ID.reset(context_token)

    def close(self):
        if self._logger is None:
            return
        for handler in list(self._logger.handlers):
            handler.flush()
            handler.close()
            self._logger.removeHandler(handler)
