import asyncio
import inspect
import os
import sqlite3
import tempfile
import unittest
from datetime import date, datetime, timezone
from unittest.mock import AsyncMock, patch

import server
import manager_server
from mailbox_store import MailboxStore
from mailbox_search import search_mailbox
from manager_server import MailboxDeleteRequest, MailboxUpdate


class FakeDecay:
    async def ensure_started(self):
        return None


class FakeDehydrator:
    async def analyze(self, _content):
        return {
            "domain": ["测试"],
            "valence": 0.5,
            "arousal": 0.3,
            "tags": [],
            "suggested_name": "测试归档",
        }

    async def digest(self, _content):
        return [{
            "content": "只进入事实桶的内容",
            "domain": ["测试"],
            "tags": [],
            "importance": 5,
            "valence": 0.5,
            "arousal": 0.3,
            "name": "长归档",
        }]


class FakeMailbox:
    def __init__(self):
        self.add = AsyncMock(return_value={
            "message_id": 7,
            "created_at": "2026-07-16T01:02:03+00:00",
            "message": "写给下一个窗口",
            "source_tool": "grow",
        })


class MailboxStoreTests(unittest.IsolatedAsyncioTestCase):
    async def test_hybrid_search_finds_old_mail_and_keyword_fallback_works(self):
        class FakeEmbeddingIndex:
            enabled = True

            async def query_segment_matches(self, query, buckets):
                scores = {}
                for bucket in buckets:
                    if bucket["id"] == "mailbox:1":
                        scores[bucket["id"]] = {"score": 0.82}
                    else:
                        scores[bucket["id"]] = {"score": 0.12}
                return scores, object()

        class BrokenEmbeddingIndex:
            enabled = True

            async def query_segment_matches(self, query, buckets):
                raise RuntimeError("model unavailable")

        with tempfile.TemporaryDirectory() as root:
            store = MailboxStore({
                "buckets_dir": root,
                "mailbox": {"db_path": os.path.join(root, "mailbox.sqlite3")},
            })
            await store.add("她搬完家以后终于松了一口气。")
            await store.add("晚上一起吃了热饭。")
            semantic = await search_mailbox(
                store, FakeEmbeddingIndex(), "换住处那天", limit=5
            )
            fallback = await search_mailbox(
                store, BrokenEmbeddingIndex(), "热饭", limit=5
            )

        self.assertEqual(semantic[0]["message"], "她搬完家以后终于松了一口气。")
        self.assertGreater(semantic[0]["semantic_score"], 0.8)
        self.assertEqual(fallback[0]["message"], "晚上一起吃了热饭。")
        self.assertIsNone(fallback[0]["semantic_score"])

    async def test_retention_soft_deletes_old_messages_but_keeps_newest(self):
        with tempfile.TemporaryDirectory() as root:
            store = MailboxStore({
                "buckets_dir": root,
                "mailbox": {
                    "db_path": os.path.join(root, "mailbox.sqlite3"),
                    "retention_days": 0,
                },
            })
            first = await store.add(
                "old message", created_at="2026-07-01T08:00:00+08:00"
            )
            recent = await store.add(
                "recent message", created_at="2026-07-08T08:00:00+08:00"
            )
            newest = await store.add(
                "newest handoff", created_at="2026-07-02T08:00:00+08:00"
            )

            store.retention_days = 5
            expired = await store.expire_old_messages(
                datetime(2026, 7, 10, tzinfo=timezone.utc)
            )
            visible = await asyncio.to_thread(store._list_sync, 10, 0, False)
            deleted = await store.get(first["message_id"], include_deleted=True)
            history = await store.history(first["message_id"])

        self.assertEqual(expired, [first["message_id"]])
        self.assertEqual(
            [item["message_id"] for item in visible],
            [newest["message_id"], recent["message_id"]],
        )
        self.assertIsNotNone(deleted["deleted_at"])
        self.assertEqual(history[0]["operation"], "delete")
        self.assertEqual(history[0]["message"], "old message")

    async def test_append_only_history_and_pagination(self):
        with tempfile.TemporaryDirectory() as root:
            store = MailboxStore({
                "buckets_dir": root,
                "mailbox": {"db_path": os.path.join(root, "mailbox.sqlite3")},
            })
            first = await store.add("第一封", created_at="2026-01-01T00:00:00+00:00")
            second = await store.add("第二封", created_at="2026-01-02T00:00:00+00:00")
            third = await store.add("第三封", created_at="2026-01-03T00:00:00+00:00")

            latest = await store.latest()
            page_one = await store.list(limit=2)
            page_two = await store.list(limit=2, before_id=second["message_id"])

            self.assertEqual(store.count(), 3)

        self.assertEqual(latest["message"], "第三封")
        self.assertEqual([item["message"] for item in page_one], ["第三封", "第二封"])
        self.assertEqual([item["message"] for item in page_two], ["第一封"])
        self.assertEqual(first["message_id"], 1)
        self.assertEqual(third["message_id"], 3)

    async def test_existing_schema_migrates_without_changing_message(self):
        with tempfile.TemporaryDirectory() as root:
            db_path = os.path.join(root, "mailbox.sqlite3")
            connection = sqlite3.connect(db_path)
            connection.execute(
                """
                CREATE TABLE mailbox_messages (
                    message_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    created_at TEXT NOT NULL,
                    message TEXT NOT NULL,
                    source_tool TEXT NOT NULL DEFAULT 'grow'
                )
                """
            )
            connection.execute(
                "INSERT INTO mailbox_messages "
                "(created_at, message, source_tool) VALUES (?, ?, ?)",
                ("2026-01-01T00:00:00+00:00", "原始留言", "grow"),
            )
            connection.commit()
            connection.close()

            store = MailboxStore({
                "buckets_dir": root,
                "mailbox": {"db_path": db_path},
            })
            item = await store.get(1)
            connection = sqlite3.connect(db_path)
            try:
                columns = {
                    row[1]
                    for row in connection.execute(
                        "PRAGMA table_info(mailbox_messages)"
                    )
                }
                history_table = connection.execute(
                    "SELECT 1 FROM sqlite_master "
                    "WHERE type = 'table' AND name = 'mailbox_message_history'"
                ).fetchone()
            finally:
                connection.close()

        self.assertEqual(item["message"], "原始留言")
        self.assertEqual(item["created_at"], "2026-01-01T00:00:00+00:00")
        self.assertIsNone(item["updated_at"])
        self.assertIsNone(item["deleted_at"])
        self.assertIn("updated_at", columns)
        self.assertIn("deleted_at", columns)
        self.assertIsNotNone(history_table)

    async def test_update_delete_and_history_are_atomic(self):
        with tempfile.TemporaryDirectory() as root:
            store = MailboxStore({
                "buckets_dir": root,
                "mailbox": {"db_path": os.path.join(root, "mailbox.sqlite3")},
            })
            first = await store.add("第一封原文")
            second = await store.add("第二封原文")

            updated = await store.update(first["message_id"], "第一封修改后")
            update_history = await store.history(first["message_id"])
            deleted = await store.delete(second["message_id"])
            delete_history = await store.history(second["message_id"])
            visible = await store.list()
            all_messages = await store.list(include_deleted=True)
            latest = await store.latest()

            self.assertEqual(store.count(), 1)
            self.assertEqual(store.count(include_deleted=True), 2)

        self.assertEqual(updated["message"], "第一封修改后")
        self.assertIsNotNone(updated["updated_at"])
        self.assertEqual(update_history[0]["operation"], "update")
        self.assertEqual(update_history[0]["message"], "第一封原文")
        self.assertIsNotNone(deleted["deleted_at"])
        self.assertEqual(delete_history[0]["operation"], "delete")
        self.assertEqual(delete_history[0]["message"], "第二封原文")
        self.assertEqual([item["message"] for item in visible], ["第一封修改后"])
        self.assertEqual(len(all_messages), 2)
        self.assertEqual(latest["message"], "第一封修改后")

    async def test_mailbox_tool_previews_then_confirms_writes(self):
        with tempfile.TemporaryDirectory() as root:
            store = MailboxStore({
                "buckets_dir": root,
                "mailbox": {"db_path": os.path.join(root, "mailbox.sqlite3")},
            })
            fallback = await store.add("较早留言")
            target = await store.add("待修改留言")
            with (
                patch.object(server, "mailbox_store", store),
                patch.object(
                    server, "_append_or_create", new=AsyncMock()
                ) as bucket_write,
                patch.dict(
                    os.environ, {server.RESPONSE_SEAL_ENV: "test-seal"}
                ),
            ):
                created = await server.mailbox(message="只写信箱，不建记忆桶")
                created_item = await store.latest()
                preview = await server.mailbox(
                    message_id=target["message_id"], message="修改后的留言"
                )
                unchanged = await store.get(target["message_id"])
                changed = await server.mailbox(
                    message_id=target["message_id"],
                    message="修改后的留言",
                    confirm=True,
                )
                delete_preview = await server.mailbox(
                    message_id=target["message_id"], delete=True
                )
                still_visible = await store.get(target["message_id"])
                deleted = await server.mailbox(
                    message_id=target["message_id"],
                    delete=True,
                    confirm=True,
                )
                default_list = await server.mailbox()
                deleted_item = await server.mailbox(
                    message_id=target["message_id"], include_deleted=True
                )
                history = await server.mailbox(
                    message_id=target["message_id"], history=True
                )
                pulse_latest = await server._pulse_boot_mailbox_section()

        self.assertIn("留言已单独存入信箱 #", created)
        self.assertIn("未创建或修改任何记忆桶", created)
        bucket_write.assert_not_awaited()
        self.assertIn("【信箱修改演习】", preview)
        self.assertEqual(unchanged["message"], "待修改留言")
        self.assertIn("留言 #2 已修改", changed)
        self.assertIn("【信箱删除演习】", delete_preview)
        self.assertEqual(still_visible["message"], "修改后的留言")
        self.assertIn("留言 #2 已删除", deleted)
        self.assertNotIn("修改后的留言", default_list)
        self.assertIn("删除时间", deleted_item)
        self.assertIn("修改前快照", history)
        self.assertIn("删除前快照", history)
        self.assertIn("待修改留言", history)
        self.assertIn("修改后的留言", history)
        self.assertIn(f"message_id: {created_item['message_id']}", pulse_latest)
        self.assertIn("只写信箱，不建记忆桶", pulse_latest)

    async def test_short_grow_writes_one_whole_bucket_and_separate_message(self):
        mailbox = FakeMailbox()
        append_or_create = AsyncMock(return_value=("测试归档", False))
        with (
            patch.object(server, "decay_engine", FakeDecay()),
            patch.object(server, "dehydrator", FakeDehydrator()),
            patch.object(server, "_check_conflicts", new=AsyncMock(return_value=[])),
            patch.object(server, "_append_or_create", new=append_or_create),
            patch.object(server, "mailbox_store", mailbox),
            patch.object(server, "_prospective_today", return_value=date(2026, 7, 15)),
        ):
            result = await server.grow("短归档内容", message="写给下一个窗口")

        append_or_create.assert_awaited_once()
        self.assertEqual(
            append_or_create.await_args.kwargs["content"],
            "【2026-07-15】\n短归档内容",
        )
        mailbox.add.assert_awaited_once_with("写给下一个窗口", source_tool="grow")
        self.assertIn("新建→测试归档", result)
        self.assertIn("留言已存入信箱 #7", result)

    async def test_long_grow_writes_exactly_one_whole_bucket(self):
        mailbox = FakeMailbox()
        append_or_create = AsyncMock(return_value=("长归档", False))
        digest = AsyncMock()
        content = "这是一段明确超过三十个字符的对话归档内容，用来验证整封保存，不再拆成多个事实桶。"
        with (
            patch.object(server, "decay_engine", FakeDecay()),
            patch.object(server, "dehydrator", FakeDehydrator()),
            patch.object(server.dehydrator, "digest", new=digest),
            patch.object(server, "_check_conflicts", new=AsyncMock(return_value=[])),
            patch.object(server, "_append_or_create", new=append_or_create),
            patch.object(server, "mailbox_store", mailbox),
            patch.object(server, "_prospective_today", return_value=date(2026, 7, 15)),
        ):
            result = await server.grow(content, message="接力留言")

        append_or_create.assert_awaited_once()
        self.assertEqual(
            append_or_create.await_args.kwargs["content"],
            f"【2026-07-15】\n{content}",
        )
        digest.assert_not_awaited()
        mailbox.add.assert_awaited_once_with("接力留言", source_tool="grow")
        self.assertIn("新建→长归档", result)
        self.assertIn("留言已存入信箱 #7", result)

    async def test_invalid_archive_does_not_store_message(self):
        mailbox = FakeMailbox()
        with (
            patch.object(server, "decay_engine", FakeDecay()),
            patch.object(server, "mailbox_store", mailbox),
        ):
            result = await server.grow("", message="不能孤立保存")

        self.assertEqual(result, "内容为空，无法整理。")
        mailbox.add.assert_not_awaited()

    async def test_mailbox_tool_and_pulse_boot_latest(self):
        class ReadMailbox:
            async def list(self, limit=10, before_id=0):
                return [{
                    "message_id": 12,
                    "created_at": "2026-07-16T08:00:00+00:00",
                    "message": "完整留言原文",
                    "source_tool": "grow",
                }]

            async def latest(self):
                return (await self.list())[0]

        with (
            patch.object(server, "mailbox_store", ReadMailbox()),
            patch.dict(os.environ, {server.RESPONSE_SEAL_ENV: "test-seal"}),
        ):
            history = await server.mailbox()
            latest = await server._pulse_boot_mailbox_section()

        self.assertIn("message_id: 12", history)
        self.assertIn("完整留言原文", history)
        self.assertTrue(history.endswith("\nseal: test-seal"))
        self.assertIn("时间: 2026-07-16T08:00:00+00:00", latest)
        self.assertIn("完整留言原文", latest)

    async def test_pulse_preview_truncates_but_history_keeps_full_text(self):
        long_message = "长" * 1200

        class LongMailbox:
            async def latest(self):
                return {
                    "message_id": 1,
                    "created_at": "2026-07-16T08:00:00+00:00",
                    "message": long_message,
                    "source_tool": "grow",
                }

        with (
            patch.object(server, "mailbox_store", LongMailbox()),
            patch.dict(server.config, {"mailbox": {"pulse_preview_chars": 1000}}),
        ):
            latest = await server._pulse_boot_mailbox_section()

        self.assertLess(len(latest), len(long_message) + 100)
        self.assertIn("请使用 mailbox() 查看全文", latest)

    def test_signatures_are_additive(self):
        self.assertEqual(
            str(inspect.signature(server.grow)),
            "(content: str, message: str = '') -> str",
        )
        self.assertEqual(
            str(inspect.signature(server.mailbox)),
            "(limit: int = 10, before_id: int = 0, message_id: int = 0, "
            "message: str = '', delete: bool = False, confirm: bool = False, "
            "history: bool = False, include_deleted: bool = False, "
            "query: str = '') -> str",
        )
        self.assertEqual(
            server.mailbox.__doc__,
            "mailbox search read write edit delete 搜索/读取/写入/修改信箱留言",
        )


class MailboxManagerApiTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = MailboxStore({
            "buckets_dir": self.temp.name,
            "mailbox": {
                "db_path": os.path.join(self.temp.name, "mailbox.sqlite3")
            },
        })
        self.store_patch = patch.object(
            manager_server, "mailbox_store", self.store
        )
        self.store_patch.start()

    async def asyncTearDown(self):
        self.store_patch.stop()
        self.temp.cleanup()

    async def test_manager_list_update_delete_and_history(self):
        first = await self.store.add("管理页第一封")
        second = await self.store.add("管理页第二封")

        listed = await manager_server.mailbox_messages(
            limit=20, before_id=0, include_deleted=False
        )
        self.assertEqual(listed["count"], 2)
        self.assertEqual(
            [item["message_id"] for item in listed["items"]],
            [second["message_id"], first["message_id"]],
        )

        updated = await manager_server.update_mailbox_message(
            second["message_id"], MailboxUpdate(message="管理页修改后")
        )
        self.assertTrue(updated["snapshot_created"])
        self.assertEqual(updated["item"]["message"], "管理页修改后")

        history = await manager_server.mailbox_message_history(
            second["message_id"], limit=20
        )
        self.assertEqual(len(history["items"]), 1)
        self.assertEqual(history["items"][0]["message"], "管理页第二封")

        deleted = await manager_server.delete_mailbox_message(
            second["message_id"],
            MailboxDeleteRequest(confirm_message_id=second["message_id"]),
        )
        self.assertTrue(deleted["snapshot_created"])
        remaining = await manager_server.mailbox_messages(
            limit=20, before_id=0, include_deleted=False
        )
        self.assertEqual(
            [item["message_id"] for item in remaining["items"]],
            [first["message_id"]],
        )
        history = await manager_server.mailbox_message_history(
            second["message_id"], limit=20
        )
        self.assertEqual(len(history["items"]), 2)


if __name__ == "__main__":
    unittest.main()
