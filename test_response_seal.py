import asyncio
import os
import unittest
from unittest.mock import AsyncMock, patch

import server


class ResponseSealTests(unittest.TestCase):
    def test_helper_uses_environment_value(self):
        with patch.dict(os.environ, {server.RESPONSE_SEAL_ENV: "test-secret"}):
            self.assertEqual(server._with_response_seal("body"), "body\nseal: test-secret")

    def test_helper_reports_missing_environment_value(self):
        with patch.dict(os.environ, {}, clear=True):
            result = server._with_response_seal("body")
        self.assertEqual(
            result,
            "body\nseal: [ERROR: OMBRE_RESPONSE_SEAL_NOT_CONFIGURED]",
        )

    def test_breath_empty_result_is_sealed(self):
        with (
            patch.dict(os.environ, {server.RESPONSE_SEAL_ENV: "test-secret"}),
            patch.object(server.decay_engine, "ensure_started", new=AsyncMock()),
            patch.object(server.bucket_mgr, "search", new=AsyncMock(return_value=[])),
            patch.object(server.random, "random", return_value=1.0),
        ):
            result = asyncio.run(server.breath(query="nothing"))
        self.assertTrue(result.endswith("\nseal: test-secret"))

    def test_breath_error_is_sealed(self):
        with (
            patch.dict(os.environ, {server.RESPONSE_SEAL_ENV: "test-secret"}),
            patch.object(server.decay_engine, "ensure_started", new=AsyncMock()),
            patch.object(server.bucket_mgr, "search", new=AsyncMock(side_effect=RuntimeError("boom"))),
        ):
            result = asyncio.run(server.breath(query="failure"))
        self.assertTrue(result.endswith("\nseal: test-secret"))

    def test_pulse_is_sealed(self):
        stats = {
            "permanent_count": 0,
            "dynamic_count": 0,
            "archive_count": 0,
            "total_size_kb": 0.0,
        }
        with (
            patch.dict(os.environ, {server.RESPONSE_SEAL_ENV: "test-secret"}),
            patch.object(server.bucket_mgr, "get_stats", new=AsyncMock(return_value=stats)),
            patch.object(server.bucket_mgr, "list_all", new=AsyncMock(return_value=[])),
        ):
            result = asyncio.run(server.pulse())
        self.assertTrue(result.endswith("\nseal: test-secret"))


if __name__ == "__main__":
    unittest.main()
