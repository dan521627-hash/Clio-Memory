import os
import re
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

import server


def bucket(bucket_id: str, content: str, *, sealed: bool = False) -> dict:
    return {
        "id": bucket_id,
        "content": content,
        "metadata": {
            "name": f"记忆-{bucket_id}",
            "type": "dynamic",
            "sealed": sealed,
            "domain": ["测试"],
            "tags": ["分页"],
            "importance": 5,
            "valence": 0.5,
            "arousal": 0.3,
        },
    }


def page_count(response: str) -> int:
    match = re.search(r"当前页: \d+/(\d+)", response)
    if not match:
        raise AssertionError("response has no pagination metadata")
    return int(match.group(1))


def page_content(response: str) -> str:
    marker = "=== 本页内容 ===\n"
    return response.split(marker, 1)[1].rsplit("\nseal: ", 1)[0]


def content_id(response: str) -> str:
    match = re.search(r"内容编号: ([0-9a-f]{12})", response)
    if not match:
        raise AssertionError("response has no content ID")
    return match.group(1)


class PaginationTests(unittest.IsolatedAsyncioTestCase):
    def test_tool_descriptions_recommend_balanced_page_size(self):
        self.assertIn("page_size=2500", server.pulse.__doc__)
        self.assertIn("page_size=2500", server.recall.__doc__)

    async def test_recall_default_returns_latest_segment_view(self):
        item = bucket("visible", "原文内容")
        manager = MagicMock()
        manager.get = AsyncMock(return_value=item)
        with (
            patch.object(server, "bucket_mgr", manager),
            patch.dict(os.environ, {server.RESPONSE_SEAL_ENV: "test-seal"}),
        ):
            result = await server.recall("visible")
            positional = await server.recall("visible", False)

        self.assertEqual(result, positional)
        self.assertIn("=== 最新记忆包 ===", result)
        self.assertIn("总包数: 1", result)
        self.assertIn("当前 before_id: p0001", result)
        self.assertIn("[记忆包 p0001 | 1/1 | 初始正文]\n原文内容", result)
        self.assertTrue(result.endswith("\nseal: test-seal"))

    async def test_recall_before_id_walks_back_one_packet_at_a_time(self):
        original = (
            "最早内容\n"
            "--- 2026-07-20T09:00 ---\n中间内容\n"
            "--- 2026-07-21T09:00 ---\n最新内容"
        )
        manager = MagicMock()
        manager.get = AsyncMock(return_value=bucket("packets", original))
        with (
            patch.object(server, "bucket_mgr", manager),
            patch.dict(os.environ, {server.RESPONSE_SEAL_ENV: "test-seal"}),
        ):
            latest = await server.recall("packets")
            older = await server.recall("packets", before_id="p0003")

        self.assertIn("最新内容", latest)
        self.assertNotIn("中间内容", latest)
        self.assertIn("中间内容", older)
        self.assertNotIn("最早内容", older)
        self.assertIn("当前 before_id: p0002", older)

    async def test_recall_segment_pages_start_from_latest_and_keep_snapshot(self):
        original = (
            "初始正文\n\n"
            "--- 2026-07-17T09:00 ---\n第一段\n\n"
            "--- 2026-07-18T09:00 ---\n第二段\n\n"
            "--- 2026-07-19T09:00 ---\n第三段\n\n"
            "--- 2026-07-20T09:00 ---\n第四段"
        )
        manager = MagicMock()
        manager.get = AsyncMock(return_value=bucket("segments", original))
        with (
            patch.object(server, "bucket_mgr", manager),
            patch.dict(os.environ, {server.RESPONSE_SEAL_ENV: "test-seal"}),
        ):
            first = await server.recall("segments", segments_per_page=2)
            snapshot_id = content_id(first)
            manager.get.return_value = bucket("segments", "后来发生变化")
            second = await server.recall(
                "segments", page=2, segments_per_page=2, content_id=snapshot_id
            )
            third = await server.recall(
                "segments", page=3, segments_per_page=2, content_id=snapshot_id
            )

        self.assertIn("第四段", first)
        self.assertIn("第三段", first)
        self.assertNotIn("第二段", first)
        self.assertIn("第二段", second)
        self.assertIn("第一段", second)
        self.assertIn("初始正文", third)
        self.assertNotIn("后来发生变化", second + third)
        self.assertEqual(manager.get.await_count, 1)

    async def test_recall_pages_reassemble_without_loss(self):
        original = "第一段\n" + ("甲" * 700) + "\n第二段\n" + ("乙" * 700)
        item = bucket("long-memory", original)
        manager = MagicMock()
        manager.get = AsyncMock(return_value=item)
        with (
            patch.object(server, "bucket_mgr", manager),
            patch.dict(os.environ, {server.RESPONSE_SEAL_ENV: "test-seal"}),
        ):
            first = await server.recall("long-memory", page_size=500)
            responses = [first]
            snapshot_id = content_id(first)
            for page in range(2, page_count(first) + 1):
                responses.append(
                    await server.recall(
                        "long-memory",
                        page=page,
                        page_size=500,
                        content_id=snapshot_id,
                    )
                )

        expected_body = (
            "bucket_id: long-memory\n名称: 记忆-long-memory\n类型: dynamic\n"
            f"完整原文:\n{original}"
        )
        self.assertEqual("".join(page_content(item) for item in responses), expected_body)
        self.assertTrue(all(item.endswith("\nseal: test-seal") for item in responses))
        content_ids = {
            re.search(r"内容编号: ([0-9a-f]{12})", item).group(1)
            for item in responses
        }
        self.assertEqual(len(content_ids), 1)
        self.assertIn("page=2, page_size=500", first)
        self.assertIn(f'content_id="{snapshot_id}"', first)
        self.assertIn("已到最后一页", responses[-1])

    async def test_pulse_pages_reassemble_and_keep_legacy_default(self):
        items = [bucket(f"bucket-{index:02d}", "内容") for index in range(40)]
        manager = MagicMock()
        manager.get_stats = AsyncMock(
            return_value={
                "permanent_count": 0,
                "dynamic_count": 40,
                "archive_count": 0,
                "total_size_kb": 10.0,
            }
        )
        manager.list_all = AsyncMock(return_value=items)
        with (
            patch.object(server, "bucket_mgr", manager),
            patch.dict(os.environ, {server.RESPONSE_SEAL_ENV: "test-seal"}),
        ):
            legacy = await server.pulse()
            first = await server.pulse(page_size=500)
            responses = [first]
            snapshot_id = content_id(first)
            for page in range(2, page_count(first) + 1):
                responses.append(
                    await server.pulse(
                        page=page, page_size=500, content_id=snapshot_id
                    )
                )

        self.assertNotIn("分页信息", legacy)
        legacy_body = legacy.rsplit("\nseal: ", 1)[0]
        self.assertEqual("".join(page_content(item) for item in responses), legacy_body)
        self.assertIn("pulse(include_archive=false, page=2, page_size=500", first)
        self.assertIn(f'content_id="{snapshot_id}"', first)
        self.assertTrue(all(item.endswith("\nseal: test-seal") for item in responses))

    async def test_invalid_pages_are_clear_and_sealed(self):
        item = bucket("visible", "原文" * 300)
        manager = MagicMock()
        manager.get = AsyncMock(return_value=item)
        with (
            patch.object(server, "bucket_mgr", manager),
            patch.dict(os.environ, {server.RESPONSE_SEAL_ENV: "test-seal"}),
        ):
            zero = await server.recall("visible", page=0, page_size=500)
            missing_size = await server.recall("visible", page=2)
            too_small = await server.recall("visible", page_size=100)
            first = await server.recall("visible", page_size=500)
            missing_id = await server.recall("visible", page=2, page_size=500)
            too_far = await server.recall(
                "visible", page=99, page_size=500, content_id=content_id(first)
            )

        self.assertIn("page 必须从 1 开始", zero)
        self.assertIn("必须带上上一页返回的 content_id", missing_size)
        self.assertIn("200-8000", too_small)
        self.assertIn("必须带上上一页返回的 content_id", missing_id)
        self.assertIn("第 99 页不存在", too_far)
        self.assertTrue(
            all(
                result.endswith("\nseal: test-seal")
                for result in (zero, missing_size, too_small, missing_id, too_far)
            )
        )

    async def test_pulse_uses_one_frozen_snapshot_for_later_pages(self):
        first_items = [bucket(f"first-{index:02d}", "内容") for index in range(40)]
        changed_items = [bucket(f"changed-{index:02d}", "变化") for index in range(40)]
        manager = MagicMock()
        manager.get_stats = AsyncMock(
            return_value={
                "permanent_count": 0,
                "dynamic_count": 40,
                "archive_count": 0,
                "total_size_kb": 10.0,
            }
        )
        manager.list_all = AsyncMock(side_effect=[first_items, changed_items])
        with (
            patch.object(server, "bucket_mgr", manager),
            patch.dict(os.environ, {server.RESPONSE_SEAL_ENV: "test-seal"}),
        ):
            first = await server.pulse(page_size=500)
            second = await server.pulse(
                page=2, page_size=500, content_id=content_id(first)
            )

        self.assertEqual(content_id(first), content_id(second))
        self.assertEqual(manager.list_all.await_count, 1)
        self.assertNotIn("changed-", second)

    async def test_sealed_recall_stays_hidden_until_explicitly_allowed(self):
        item = bucket("sealed", "封存秘密" * 300, sealed=True)
        manager = MagicMock()
        manager.get = AsyncMock(return_value=item)
        with (
            patch.object(server, "bucket_mgr", manager),
            patch.dict(os.environ, {server.RESPONSE_SEAL_ENV: "test-seal"}),
        ):
            hidden = await server.recall("sealed", page_size=500)
            visible = await server.recall(
                "sealed", include_sealed=True, page_size=500
            )

        self.assertNotIn("封存秘密", hidden)
        self.assertNotIn("分页信息", hidden)
        self.assertIn("封存秘密", visible)
        self.assertIn("分页信息", visible)
        self.assertTrue(hidden.endswith("\nseal: test-seal"))
        self.assertTrue(visible.endswith("\nseal: test-seal"))


if __name__ == "__main__":
    unittest.main()
