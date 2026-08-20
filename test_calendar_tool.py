import inspect
import os
import unittest
from unittest.mock import AsyncMock, patch

import server


class CalendarToolTests(unittest.IsolatedAsyncioTestCase):
    def test_signature_is_read_only_and_backward_safe(self):
        signature = inspect.signature(server.calendar)
        self.assertEqual(
            list(signature.parameters),
            ["date", "include_archived", "include_sealed"],
        )
        self.assertEqual(signature.parameters["date"].default, "")
        self.assertFalse(signature.parameters["include_archived"].default)
        self.assertFalse(signature.parameters["include_sealed"].default)

    async def test_invalid_date_is_rejected_without_reading_stores(self):
        bucket_list = AsyncMock()
        with (
            patch.object(server.bucket_mgr, "list_all", new=bucket_list),
            patch.dict(os.environ, {server.RESPONSE_SEAL_ENV: "test-seal"}),
        ):
            result = await server.calendar("2026-02-31")

        self.assertIn("日期必须使用有效的 YYYY-MM-DD", result)
        self.assertIn("seal: test-seal", result)
        bucket_list.assert_not_awaited()

    async def test_date_read_uses_all_sources_and_returns_bucket_id(self):
        bucket = {
            "id": "bucket-42",
            "metadata": {
                "name": "八月十一日",
                "created": "2026-08-11T08:00:00+08:00",
            },
            "content": "这一天留下的原文",
        }
        mocks = {
            "buckets": AsyncMock(return_value=[bucket]),
            "mailbox": AsyncMock(return_value=[]),
            "behavior": AsyncMock(return_value=[]),
            "tasks": AsyncMock(return_value=[]),
            "treasury": AsyncMock(return_value=[]),
            "thoughts": AsyncMock(return_value=[]),
            "darkflow": AsyncMock(return_value=None),
            "facts": AsyncMock(return_value=[]),
        }
        with (
            patch.object(server.bucket_mgr, "list_all", new=mocks["buckets"]),
            patch.object(
                server.mailbox_store, "search_pool", new=mocks["mailbox"]
            ),
            patch.object(
                server.behavior_service.store, "list", new=mocks["behavior"]
            ),
            patch.object(server.task_service.store, "list", new=mocks["tasks"]),
            patch.object(server.treasury_store, "list", new=mocks["treasury"]),
            patch.object(
                server.xinchao_service,
                "list_private_thoughts",
                new=mocks["thoughts"],
            ),
            patch.object(
                server.xinchao_service, "darkflow_status", new=mocks["darkflow"]
            ),
            patch.object(
                server.fact_timeline_store, "list_facts", new=mocks["facts"]
            ),
            patch.dict(os.environ, {server.RESPONSE_SEAL_ENV: "test-seal"}),
        ):
            result = await server.calendar("2026-08-11")

        self.assertIn("【2026-08-11】共 1 条", result)
        self.assertIn("bucket_id=bucket-42", result)
        self.assertIn("这一天留下的原文", result)
        self.assertIn("seal: test-seal", result)
        mocks["buckets"].assert_awaited_once_with(
            include_archive=False,
            include_sealed=False,
        )
        mocks["mailbox"].assert_awaited_once_with(
            include_deleted=False,
            limit=5000,
        )


if __name__ == "__main__":
    unittest.main()
