import asyncio
import json
import tempfile
import unittest
from pathlib import Path

from request_diagnostics import (
    MCPRequestDiagnosticsMiddleware,
    current_mcp_event_id,
    current_mcp_session_id,
)


class DiagnosticMiddlewareTests(unittest.TestCase):
    @staticmethod
    async def _app(scope, receive, send):
        await receive()
        headers = []
        if scope["method"] == "POST" and not any(
            key.lower() == b"mcp-session-id" for key, _ in scope.get("headers", [])
        ):
            headers.append((b"mcp-session-id", b"abcdef1234567890"))
        await send({"type": "http.response.start", "status": 200, "headers": headers})
        await send({"type": "http.response.body", "body": b"{}"})

    @staticmethod
    async def _request(middleware, method, body, session_id=""):
        headers = [(b"user-agent", b"diagnostic-test/1.0")]
        if session_id:
            headers.append((b"mcp-session-id", session_id.encode()))
        scope = {
            "type": "http",
            "path": "/mcp",
            "method": method,
            "headers": headers,
        }
        received = False

        async def receive():
            nonlocal received
            if not received:
                received = True
                return {"type": "http.request", "body": body, "more_body": False}
            return {"type": "http.disconnect"}

        async def send(_message):
            return None

        await middleware(scope, receive, send)

    @staticmethod
    async def _disconnecting_app(scope, receive, send):
        await receive()
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send(
            {
                "type": "http.response.body",
                "body": b"partial",
                "more_body": True,
            }
        )
        await receive()
        raise asyncio.CancelledError()

    def test_logs_only_limited_metadata_and_inherits_client_info(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            log_path = Path(temp_dir) / "mcp_requests.log"
            middleware = MCPRequestDiagnosticsMiddleware(
                self._app, log_path=str(log_path), max_bytes=4096, backup_count=1
            )
            initialize = json.dumps({
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "clientInfo": {"name": "Claude", "version": "9.9"},
                    "capabilities": {},
                },
            }).encode()
            tool_call = json.dumps({
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {"name": "breath", "arguments": {"secret": "DO-NOT-LOG"}},
            }).encode()

            asyncio.run(self._request(middleware, "POST", initialize))
            asyncio.run(
                self._request(
                    middleware, "POST", tool_call, session_id="abcdef1234567890"
                )
            )
            middleware.close()

            raw = log_path.read_text(encoding="utf-8")
            records = [json.loads(line) for line in raw.splitlines()]

        self.assertEqual([item["method"] for item in records], ["initialize", "tools/call"])
        self.assertFalse(records[0]["has_session_header"])
        self.assertTrue(records[1]["has_session_header"])
        self.assertEqual(records[0]["session_id_prefix"], "abcdef")
        self.assertEqual(records[1]["session_id_prefix"], "abcdef")
        self.assertEqual(records[1]["client_info"], {"name": "Claude", "version": "9.9"})
        self.assertEqual(records[1]["user_agent"], "diagnostic-test/1.0")
        self.assertEqual(records[1]["tool_name"], "breath")
        self.assertGreaterEqual(records[1]["duration_ms"], 0)
        self.assertEqual(records[1]["response_body_bytes"], 2)
        self.assertTrue(records[1]["stream_completed"])
        self.assertFalse(records[1]["client_disconnected"])
        self.assertNotIn("DO-NOT-LOG", raw)
        self.assertNotIn("abcdef1234567890", raw)

    def test_logs_partial_stream_and_client_disconnect(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            log_path = Path(temp_dir) / "mcp_requests.log"
            middleware = MCPRequestDiagnosticsMiddleware(
                self._disconnecting_app,
                log_path=str(log_path),
                max_bytes=4096,
                backup_count=1,
            )
            tool_call = json.dumps({
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {"name": "pulse", "arguments": {}},
            }).encode()

            with self.assertRaises(asyncio.CancelledError):
                asyncio.run(
                    self._request(
                        middleware,
                        "POST",
                        tool_call,
                        session_id="abcdef1234567890",
                    )
                )
            middleware.close()
            record = json.loads(log_path.read_text(encoding="utf-8").strip())

        self.assertEqual(record["tool_name"], "pulse")
        self.assertGreaterEqual(record["duration_ms"], 0)
        self.assertEqual(record["response_body_bytes"], len(b"partial"))
        self.assertFalse(record["stream_completed"])
        self.assertTrue(record["client_disconnected"])

    def test_activity_callback_receives_only_safe_request_identity(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            observed = []

            async def callback(session_id, messages):
                observed.append((session_id, messages))

            async def app(scope, receive, send):
                self.assertEqual(current_mcp_session_id(), "session-safe-123")
                self.assertEqual(
                    current_mcp_event_id(), "session-safe-123\0request-77"
                )
                await self._app(scope, receive, send)

            middleware = MCPRequestDiagnosticsMiddleware(
                app,
                log_path=str(Path(temp_dir) / "mcp_requests.log"),
                activity_callback=callback,
            )
            tool_call = json.dumps({
                "jsonrpc": "2.0",
                "id": "request-77",
                "method": "tools/call",
                "params": {
                    "name": "pulse_boot",
                    "arguments": {"private": "NEVER-PASS-THIS"},
                },
            }).encode()
            asyncio.run(
                self._request(
                    middleware,
                    "POST",
                    tool_call,
                    session_id="session-safe-123",
                )
            )
            middleware.close()

        self.assertEqual(observed[0][0], "session-safe-123")
        self.assertEqual(observed[0][1][0]["tool_name"], "pulse_boot")
        self.assertEqual(observed[0][1][0]["request_id"], "request-77")
        self.assertNotIn("NEVER-PASS-THIS", json.dumps(observed))


if __name__ == "__main__":
    unittest.main()
