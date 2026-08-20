import unittest
from unittest.mock import AsyncMock, MagicMock, patch

import server
from tag_policy import CATEGORIES, classify_category, normalize_analysis, parse_category


class TagPolicyTests(unittest.TestCase):
    def test_classifier_returns_one_stable_category(self):
        examples = {
            "我是谁与存在论锚点": "核心与世界观",
            "这段恋爱和亲密关系让我吃醋": "关系与亲密",
            "今天买菜吃饭和收拾家里": "日常生活",
            "复诊、睡眠和身体照护": "健康与照护",
            "明天的待办和考试计划": "计划与事务",
            "Docker MCP 服务部署": "技术与创作",
            "去论坛社区回帖": "社交与社区",
        }
        for content, expected in examples.items():
            with self.subTest(content=content):
                self.assertEqual(classify_category(content), expected)

    def test_model_free_tags_are_replaced(self):
        result = normalize_analysis(
            "Docker 服务部署",
            {"domain": ["AI", "网络"], "tags": ["乱标签", "七月"], "valence": 0.5},
        )
        self.assertEqual(result["domain"], ["技术与创作"])
        self.assertEqual(result["tags"], ["技术与创作"])
        self.assertIn(result["category"], CATEGORIES)

    def test_manual_category_rejects_arbitrary_labels(self):
        with self.assertRaisesRegex(ValueError, "系统分类"):
            parse_category("七月,身份")
        self.assertEqual(parse_category("日常生活"), "日常生活")


class TagPolicyToolTests(unittest.IsolatedAsyncioTestCase):
    async def test_hold_ignores_caller_tags_and_writes_one_category(self):
        analyzer = MagicMock()
        analyzer.analyze = AsyncMock(
            return_value={
                "domain": ["AI"],
                "tags": ["Docker", "七月"],
                "valence": 0.5,
                "arousal": 0.3,
                "suggested_name": "技术记录",
            }
        )
        with (
            patch.object(server, "dehydrator", analyzer),
            patch.object(server.decay_engine, "ensure_started", new=AsyncMock()),
            patch.object(server, "_check_conflicts", new=AsyncMock(return_value=[])),
            patch.object(
                server,
                "_append_or_create",
                new=AsyncMock(return_value=("技术记录", True)),
            ) as writer,
        ):
            await server.hold("Docker MCP 服务部署", tags="七月,随便写")

        self.assertEqual(writer.await_args.kwargs["domain"], ["技术与创作"])
        self.assertEqual(writer.await_args.kwargs["tags"], ["技术与创作"])

    async def test_grow_classifies_once_and_writes_one_atomic_bucket(self):
        analyzer = MagicMock()
        analyzer.analyze = AsyncMock(
            return_value={
                "domain": ["论坛"],
                "tags": ["回帖", "七月"],
                "valence": 0.5,
                "arousal": 0.3,
                "suggested_name": "社区记录",
            }
        )
        analyzer.digest = AsyncMock()
        with (
            patch.object(server, "dehydrator", analyzer),
            patch.object(server.decay_engine, "ensure_started", new=AsyncMock()),
            patch.object(server, "_check_conflicts", new=AsyncMock(return_value=[])),
            patch.object(
                server,
                "_append_or_create",
                new=AsyncMock(return_value=("社区记录", False)),
            ) as writer,
        ):
            result = await server.grow("去论坛回帖")

        writer.assert_awaited_once()
        self.assertEqual(writer.await_args.kwargs["domain"], ["社交与社区"])
        self.assertEqual(writer.await_args.kwargs["tags"], ["社交与社区"])
        self.assertIn("去论坛回帖", writer.await_args.kwargs["content"])
        analyzer.digest.assert_not_awaited()
        self.assertIn("新建→社区记录", result)

    async def test_trace_rejects_free_form_tag_without_writing(self):
        manager = MagicMock()
        manager.get = AsyncMock(
            return_value={"id": "bucket", "content": "原文", "metadata": {}}
        )
        manager.update = AsyncMock(return_value=True)
        with patch.object(server, "bucket_mgr", manager):
            result = await server.trace("bucket", tags="七月")

        self.assertIn("系统分类", result)
        manager.update.assert_not_awaited()

    async def test_trace_sets_tags_and_domain_together(self):
        manager = MagicMock()
        manager.get = AsyncMock(
            return_value={"id": "bucket", "content": "原文", "metadata": {}}
        )
        manager.update = AsyncMock(return_value=True)
        with patch.object(server, "bucket_mgr", manager):
            result = await server.trace("bucket", domain="日常生活")

        self.assertIn("已修改", result)
        self.assertEqual(manager.update.await_args.kwargs["domain"], ["日常生活"])
        self.assertEqual(manager.update.await_args.kwargs["tags"], ["日常生活"])


if __name__ == "__main__":
    unittest.main()
