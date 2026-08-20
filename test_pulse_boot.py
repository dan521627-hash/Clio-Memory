import inspect
import os
import tempfile
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

import server
from bucket_manager import BucketManager


def bucket(bucket_id, name, *, bucket_type="dynamic", tags=None, content="", **meta):
    metadata = {
        "id": bucket_id,
        "name": name,
        "type": bucket_type,
        "tags": tags or [],
        "domain": meta.pop("domain", []),
        "created": meta.pop("created", "2026-01-01T00:00:00"),
        "last_active": meta.pop("last_active", "2026-01-01T00:00:00"),
        **meta,
    }
    return {"id": bucket_id, "metadata": metadata, "content": content}


class FakeManager:
    def __init__(self, buckets):
        self.buckets = buckets
        self.list_all = AsyncMock(return_value=buckets)


class FakeDehydrator:
    def __init__(self, failing_id=""):
        self.failing_id = failing_id
        self.calls = []

    async def dehydrate(self, _content, metadata):
        self.calls.append(metadata["id"])
        if metadata["id"] == self.failing_id:
            raise RuntimeError("summary unavailable")
        return f"📌 记忆桶: {metadata['name']}\n" + ("摘要" * 200)


class PulseBootTests(unittest.IsolatedAsyncioTestCase):
    def test_tool_guide_lists_every_callable_capability(self):
        expected_tools = {
            "breath",
            "recall",
            "calendar",
            "timeline",
            "cabinet",
            "hold",
            "grow",
            "mailbox",
            "trace",
            "split_bucket",
            "xinchao_status",
            "inner_state",
            "living_memory",
            "self_state",
            "personality_preview",
            "continuity_review",
            "tasks",
            "treasury",
            "feedback",
            "digest_preview",
            "pulse",
            "heartbeat",
            "pulse_boot",
        }

        for tool_name in expected_tools:
            self.assertIn(tool_name, server.PULSE_BOOT_TOOL_GUIDE)
        self.assertIn("不要一次读取全库", server.PULSE_BOOT_TOOL_GUIDE)

    def test_hormone_summary_hides_internal_timestamps(self):
        text = "离开计时起点（UTC+8）：2026-08-09 08:00\n想靠近：0.72\n状态时间（UTC+8）：2026-08-09 09:00"
        result = server._pulse_boot_hormone_summary(text)
        self.assertEqual(result, "想靠近：0.72")

    async def asyncSetUp(self):
        self.mailbox_patch = patch.object(
            server,
            "_pulse_boot_mailbox_section",
            new=AsyncMock(return_value=""),
        )
        self.mailbox_patch.start()
        self.mailbox_context_patch = patch.object(
            server,
            "_pulse_boot_mailbox_context",
            new=AsyncMock(return_value=None),
        )
        self.mailbox_context_patch.start()
        self.xinchao_consume_patch = patch.object(
            server.xinchao_service,
            "consume_boot",
            new=AsyncMock(return_value={"available": False}),
        )
        self.xinchao_consume_patch.start()
        self.xinchao_render_patch = patch.object(
            server.xinchao_service,
            "render_compact",
            new=MagicMock(return_value="激素暂无新变化。"),
        )
        self.xinchao_render_patch.start()
        self.thoughts_patch = patch.object(
            server.xinchao_service,
            "list_private_thoughts",
            new=AsyncMock(return_value=[]),
        )
        self.thoughts_patch.start()
        self.darkflow_pending_patch = patch.object(
            server.xinchao_service,
            "pending_darkflow",
            new=AsyncMock(return_value=None),
        )
        self.darkflow_pending_patch.start()
        self.darkflow_mark_patch = patch.object(
            server.xinchao_service,
            "mark_darkflow_delivered",
            new=AsyncMock(return_value=True),
        )
        self.darkflow_mark_patch.start()
        self.darkflow_discard_patch = patch.object(
            server.xinchao_service,
            "discard_darkflow",
            new=AsyncMock(return_value=True),
        )
        self.darkflow_discard_patch.start()
        self.latest_boot_patch = patch.object(
            server.xinchao_service,
            "latest_boot_delivery",
            new=AsyncMock(return_value=None),
        )
        self.latest_boot_patch.start()
        self.record_boot_patch = patch.object(
            server.xinchao_service,
            "record_boot_delivery",
            new=AsyncMock(),
        )
        self.record_boot_patch.start()
        self.task_counts_patch = patch.object(
            server.task_service.store,
            "counts",
            new=AsyncMock(
                return_value={"open": 0, "completed": 0, "cancelled": 0, "total": 0}
            ),
        )
        self.task_counts_patch.start()
        self.task_completions_patch = patch.object(
            server.task_service.store,
            "pending_completions",
            new=AsyncMock(return_value=[]),
        )
        self.task_completions_patch.start()
        self.timeline_pending_patch = patch.object(
            server.fact_timeline_store,
            "list_candidates",
            new=AsyncMock(return_value=[]),
        )
        self.timeline_pending_patch.start()
        self.observe_presence_patch = patch.object(
            server.xinchao_service,
            "observe_presence",
            new=AsyncMock(
                return_value={"status": "observed", "cycle_started": True}
            ),
        )
        self.observe_presence = self.observe_presence_patch.start()

    async def asyncTearDown(self):
        self.timeline_pending_patch.stop()
        self.task_completions_patch.stop()
        self.task_counts_patch.stop()
        self.record_boot_patch.stop()
        self.latest_boot_patch.stop()
        self.darkflow_discard_patch.stop()
        self.observe_presence_patch.stop()
        self.darkflow_mark_patch.stop()
        self.darkflow_pending_patch.stop()
        self.xinchao_render_patch.stop()
        self.thoughts_patch.stop()
        self.xinchao_consume_patch.stop()
        self.mailbox_context_patch.stop()
        self.mailbox_patch.stop()

    async def test_first_boot_observes_presence_without_starting_timer(self):
        with (
            patch.object(server, "bucket_mgr", FakeManager([])),
            patch.dict(server.config, {"pulse_boot": {"feeling_write_reminder": False}}),
            patch.dict(os.environ, {server.RESPONSE_SEAL_ENV: "test-seal"}),
        ):
            await server.pulse_boot()

        self.observe_presence.assert_awaited_once()
        self.assertFalse(self.observe_presence.await_args.kwargs["start_cycle"])
        self.assertEqual(
            self.observe_presence.await_args.kwargs["source"], "mcp:pulse_boot"
        )

    async def test_reused_mcp_session_still_serves_each_explicit_boot_call(self):
        manager = FakeManager([])
        with (
            patch.object(server, "bucket_mgr", manager),
            patch.object(server, "_active_mcp_session_key", return_value="shared-session"),
            patch.object(
                server.xinchao_service,
                "boot_delivery",
                new=AsyncMock(
                    return_value={"delivered_at": "2026-08-13T02:00:00+08:00"}
                ),
            ) as boot_delivery,
            patch.object(
                server.xinchao_service,
                "record_boot_delivery",
                new=AsyncMock(),
            ) as record_boot_delivery,
            patch.dict(
                server.config,
                {"pulse_boot": {"feeling_write_reminder": False}},
            ),
            patch.dict(os.environ, {server.RESPONSE_SEAL_ENV: "test-seal"}),
        ):
            first = await server.pulse_boot()
            second = await server.pulse_boot()

        self.assertIn("=== Clio 开机记忆 ===", first)
        self.assertIn("=== Clio 开机记忆 ===", second)
        self.assertNotIn("本窗口已经领取过开机资料", second)
        boot_delivery.assert_not_awaited()
        self.assertEqual(record_boot_delivery.await_count, 2)
        self.assertEqual(manager.list_all.await_count, 2)

    async def test_new_memory_section_only_shows_writes_after_previous_boot(self):
        manager = FakeManager([bucket("new-bucket", "今天的新记忆")])
        old_context = {
            "event_id": 1,
            "created_at": "2026-08-17T09:00:00+08:00",
            "source_tool": "hold",
            "source_ref": "new-bucket",
            "context_card": "旧内容不应再次出现",
        }
        new_context = {
            **old_context,
            "event_id": 2,
            "created_at": "2026-08-17T11:00:00+08:00",
            "context_card": "刚写进去的新内容",
        }
        with (
            patch.object(server, "bucket_mgr", manager),
            patch.object(
                server.xinchao_service,
                "latest_boot_delivery",
                new=AsyncMock(
                    return_value={"delivered_at": "2026-08-17T10:00:00+08:00"}
                ),
            ),
            patch.object(
                server.xinchao_service,
                "consume_boot",
                new=AsyncMock(
                    side_effect=[
                        {"available": True, "event_contexts": [old_context]},
                        {"available": True, "event_contexts": [new_context]},
                    ]
                ),
            ),
            patch.dict(os.environ, {server.RESPONSE_SEAL_ENV: "test-seal"}),
        ):
            without_new_write = await server.pulse_boot()
            with_new_write = await server.pulse_boot()

        self.assertNotIn("【新写入的记忆】", without_new_write)
        self.assertNotIn("旧内容不应再次出现", without_new_write)
        self.assertIn("【新写入的记忆】", with_new_write)
        self.assertIn("今天的新记忆", with_new_write)
        self.assertIn("刚写进去的新内容", with_new_write)

    async def test_repeated_boot_keeps_context_but_does_not_repeat_darkflow(self):
        manager = FakeManager([])
        darkflow = {
            "cycle_id": 88,
            "created_at": "2026-08-13T02:00:00+08:00",
            "content": "只应交付一次的暗涌",
            "status": "pending",
        }
        with (
            patch.object(server, "bucket_mgr", manager),
            patch.object(
                server.xinchao_service,
                "consume_boot",
                new=AsyncMock(
                    side_effect=[
                        {"available": True, "darkflow_item": darkflow},
                        {"available": False, "darkflow_item": None},
                    ]
                ),
            ),
            patch.object(
                server.xinchao_service,
                "mark_darkflow_delivered",
                new=AsyncMock(return_value=True),
            ) as mark,
            patch.dict(
                server.config,
                {"pulse_boot": {"feeling_write_reminder": False}},
            ),
            patch.dict(os.environ, {server.RESPONSE_SEAL_ENV: "test-seal"}),
        ):
            first = await server.pulse_boot()
            second = await server.pulse_boot()

        self.assertIn("【暗涌】", first)
        self.assertIn("只应交付一次的暗涌", first)
        self.assertNotIn("【暗涌】", second)
        self.assertIn("=== Clio 开机记忆 ===", second)
        self.assertIn("【按需入口】", second)
        mark.assert_awaited_once_with(88)
        self.assertEqual(manager.list_all.await_count, 2)

    async def test_darkflow_is_compact_and_latest_mailbox_still_expands(self):
        manager = FakeManager([])
        mailbox = {
            "message_id": 12,
            "created_at": "2026-08-01T08:00:00+08:00",
            "message": "上一窗口亲自留下的信",
            "source_tool": "mailbox",
        }
        darkflow = {
            "cycle_id": 4,
            "created_at": "2026-08-01T10:30:00+08:00",
            "content": "信写完以后，我又有了新的变化。",
            "status": "pending",
        }
        with (
            patch.object(server, "bucket_mgr", manager),
            patch.object(
                server,
                "_pulse_boot_mailbox_context",
                new=AsyncMock(return_value=mailbox),
            ),
            patch.object(
                server,
                "_pulse_boot_mailbox_section",
                new=AsyncMock(
                    return_value="message_id: 12\n时间: 2026-08-01T08:00:00+08:00\n上一窗口亲自留下的信"
                ),
            ),
            patch.object(
                server.xinchao_service,
                "consume_boot",
                new=AsyncMock(
                    return_value={
                        "available": True,
                        "darkflow_item": darkflow,
                    }
                ),
            ),
            patch.object(
                server.xinchao_service,
                "mark_darkflow_delivered",
                new=AsyncMock(return_value=True),
            ) as mark,
            patch.dict(
                server.config,
                {"pulse_boot": {"feeling_write_reminder": False}},
            ),
            patch.dict(os.environ, {server.RESPONSE_SEAL_ENV: "test-seal"}),
        ):
            result = await server.pulse_boot()

        self.assertNotIn("【窗口接力·主体】", result)
        self.assertIn("【暗涌】", result)
        self.assertIn("信写完以后，我又有了新的变化。", result)
        self.assertNotIn("【信箱最新留言】", result)
        self.assertIn("信箱另有最新留言", result)
        self.assertNotIn("上一窗口亲自留下的信", result)
        self.assertNotIn("【全库主题导航】", result)
        mark.assert_awaited_once_with(4)

    async def test_static_elapsed_is_hidden_before_first_darkflow(self):
        manager = FakeManager([])
        with (
            patch.object(server, "bucket_mgr", manager),
            patch.object(
                server.xinchao_service,
                "consume_boot",
                new=AsyncMock(
                    return_value={
                        "available": True,
                        "static_ready": True,
                        "elapsed_seconds": 2700,
                        "darkflow_item": None,
                    }
                ),
            ),
            patch.dict(
                server.config,
                {"pulse_boot": {"feeling_write_reminder": False}},
            ),
            patch.dict(os.environ, {server.RESPONSE_SEAL_ENV: "test-seal"}),
        ):
            result = await server.pulse_boot()

        self.assertNotIn("【静默时长】", result)
        self.assertNotIn("45分钟", result)
        self.assertNotIn("【暗涌】", result)

    async def test_stale_pending_darkflow_is_not_revived_across_cycles(self):
        manager = FakeManager([])
        stale = {
            "cycle_id": 30,
            "content": "上一轮已经结束的暗涌",
            "status": "pending",
        }
        mailbox = {
            "message_id": 72,
            "created_at": "2026-08-11T12:31:44+08:00",
            "message": "上一窗口刚写的新信",
            "source_tool": "mailbox",
        }
        with (
            patch.object(server, "bucket_mgr", manager),
            patch.object(
                server,
                "_pulse_boot_mailbox_context",
                new=AsyncMock(return_value=mailbox),
            ),
            patch.object(
                server,
                "_pulse_boot_mailbox_section",
                new=AsyncMock(return_value="上一窗口刚写的新信"),
            ),
            patch.object(
                server.xinchao_service,
                "pending_darkflow",
                new=AsyncMock(return_value=stale),
            ) as pending,
            patch.object(
                server.xinchao_service,
                "consume_boot",
                new=AsyncMock(
                    return_value={"available": True, "cycle_id": 31}
                ),
            ),
            patch.dict(
                server.config,
                {"pulse_boot": {"feeling_write_reminder": False}},
            ),
            patch.dict(os.environ, {server.RESPONSE_SEAL_ENV: "test-seal"}),
        ):
            result = await server.pulse_boot()

        pending.assert_not_awaited()
        self.assertNotIn("上一轮已经结束的暗涌", result)
        self.assertIn("上一窗口刚写的新信", result)

    async def test_boot_hands_off_pending_sent_actions_then_purges_plaintext(self):
        manager = FakeManager([])
        darkflow = {
            "cycle_id": 33,
            "created_at": "2026-08-08T20:00:00+08:00",
            "content": "本轮暗涌",
            "status": "pending",
        }
        sent = {
            "action_id": 91,
            "decided_at": "2026-08-08T21:00:00+08:00",
            "delivered_at": "2026-08-08T21:00:02+08:00",
            "content": "你什么时候回来？",
        }
        with (
            patch.object(server, "bucket_mgr", manager),
            patch.object(
                server.xinchao_service,
                "pending_darkflow",
                new=AsyncMock(return_value=darkflow),
            ),
            patch.object(
                server.behavior_service.store,
                "list_pending_handoff",
                new=AsyncMock(return_value=[sent]),
            ) as behavior_list,
            patch.object(
                server.behavior_service.store,
                "purge_handoff",
                new=AsyncMock(return_value=1),
            ) as behavior_purge,
            patch.dict(server.config, {"pulse_boot": {"feeling_write_reminder": False}}),
            patch.dict(os.environ, {server.RESPONSE_SEAL_ENV: "test-seal"}),
        ):
            result = await server.pulse_boot()

        behavior_list.assert_awaited_once_with(limit=30)
        behavior_purge.assert_awaited_once_with([91])
        self.assertIn("【静默期间已发送】", result)
        self.assertIn("已发送：你什么时候回来？", result)

    async def test_mailbox_is_shown_when_no_darkflow_exists(self):
        manager = FakeManager([])
        mailbox = {
            "message_id": 12,
            "created_at": "2026-08-01T08:00:00+08:00",
            "message": "上一窗口亲自留下的信",
            "source_tool": "mailbox",
        }
        with (
            patch.object(server, "bucket_mgr", manager),
            patch.object(
                server,
                "_pulse_boot_mailbox_context",
                new=AsyncMock(return_value=mailbox),
            ),
            patch.object(
                server,
                "_pulse_boot_mailbox_section",
                new=AsyncMock(return_value="上一窗口亲自留下的信"),
            ),
            patch.object(
                server.xinchao_service,
                "pending_darkflow",
                new=AsyncMock(return_value=None),
            ),
            patch.dict(
                server.config,
                {"pulse_boot": {"feeling_write_reminder": False}},
            ),
            patch.dict(os.environ, {server.RESPONSE_SEAL_ENV: "test-seal"}),
        ):
            result = await server.pulse_boot()

        self.assertIn("【信箱最新留言】", result)
        self.assertIn("上一窗口亲自留下的信", result)
        self.assertNotIn("当前没有待交付暗涌", result)
        self.assertNotIn("【窗口接力·主体】", result)
        self.assertNotIn("【沉默后的变化·暗涌，仅展示一次】", result)

    async def test_private_thoughts_appear_only_when_present(self):
        manager = FakeManager([])
        thoughts = [
            {
                "status": "flash",
                "thought_text": "我忽然很想知道她现在在做什么。",
            },
            {
                "status": "obsession",
                "thought_text": "这件事还在我心里反复打转。",
            },
        ]
        with (
            patch.object(server, "bucket_mgr", manager),
            patch.object(
                server.xinchao_service,
                "list_private_thoughts",
                new=AsyncMock(return_value=thoughts),
            ),
            patch.dict(
                server.config,
                {"pulse_boot": {"feeling_write_reminder": False}},
            ),
            patch.dict(os.environ, {server.RESPONSE_SEAL_ENV: "test-seal"}),
        ):
            result = await server.pulse_boot()

        self.assertIn("【心念】", result)
        self.assertIn("有 2 条心念（闪念 1，执念 1）", result)
        self.assertNotIn("我忽然很想知道她现在在做什么", result)
        self.assertNotIn("当前没有心念", result)

    async def test_empty_optional_sections_leave_no_placeholder_text(self):
        manager = FakeManager([])
        with (
            patch.object(server, "bucket_mgr", manager),
            patch.object(
                server.xinchao_service,
                "render_compact",
                new=MagicMock(return_value=""),
            ),
            patch.dict(
                server.config,
                {"pulse_boot": {"feeling_write_reminder": False}},
            ),
            patch.dict(os.environ, {server.RESPONSE_SEAL_ENV: "test-seal"}),
        ):
            result = await server.pulse_boot()

        self.assertNotIn("【心念】", result)
        self.assertNotIn("【信箱最新留言】", result)
        self.assertNotIn("【沉默后的变化·暗涌", result)
        self.assertNotIn("当前没有", result)
        self.assertNotIn("暂无", result)

    async def test_system_settlement_rows_are_not_treated_as_main_mailbox(self):
        fake_store = type(
            "Mailbox",
            (),
            {
                "list": AsyncMock(
                    return_value=[
                        {
                            "message_id": 20,
                            "message": "旧系统沉淀",
                            "source_tool": "xinchao_settlement",
                        },
                        {
                            "message_id": 19,
                            "message": "真正的窗口留言",
                            "source_tool": "mailbox",
                        },
                    ]
                )
            },
        )()
        self.mailbox_context_patch.stop()
        try:
            with patch.object(server, "mailbox_store", fake_store):
                item = await server._pulse_boot_mailbox_context()
        finally:
            self.mailbox_context_patch.start()
        self.assertEqual(item["message_id"], 19)

    async def test_includes_fixed_treasury_summary_and_ai_instruction(self):
        manager = FakeManager([])
        treasury = type(
            "FakeTreasury",
            (),
            {
                "summary": AsyncMock(
                    return_value={
                        "symbol": "¥",
                        "balance": "72.00",
                        "total_income": "100.00",
                        "total_expense": "28.00",
                        "entry_count": 2,
                    }
                )
            },
        )()
        with (
            patch.object(server, "bucket_mgr", manager),
            patch.object(server, "treasury_store", treasury),
            patch.dict(
                server.config,
                {"pulse_boot": {"feeling_write_reminder": False}},
            ),
            patch.dict(os.environ, {server.RESPONSE_SEAL_ENV: "test-seal"}),
        ):
            result = await server.pulse_boot()

        self.assertNotIn("【AI小金库】", result)

    async def test_empty_optional_sections_are_omitted(self):
        manager = FakeManager([])
        treasury = type(
            "FakeTreasury",
            (),
            {
                "summary": AsyncMock(
                    return_value={
                        "symbol": "¥",
                        "balance": "0.00",
                        "total_income": "0.00",
                        "total_expense": "0.00",
                        "entry_count": 0,
                    }
                )
            },
        )()
        with (
            patch.object(server, "bucket_mgr", manager),
            patch.object(server, "treasury_store", treasury),
            patch.object(
                server.behavior_service.store,
                "list_pending_handoff",
                new=AsyncMock(return_value=[]),
            ),
            patch.dict(
                server.config,
                {"pulse_boot": {"feeling_write_reminder": False}},
            ),
            patch.dict(os.environ, {server.RESPONSE_SEAL_ENV: "test-seal"}),
        ):
            result = await server.pulse_boot()

        self.assertNotIn("【静默期间已发送】", result)
        self.assertNotIn("没有新的 Bark", result)
        self.assertNotIn("【AI小金库】", result)
        self.assertNotIn("尚未设置开机核心记忆", result)
        self.assertIn("【按需入口】", result)

    async def test_core_pins_use_sort_order_instead_of_bucket_id(self):
        buckets = [
            bucket("000-first-by-id", "ID order", pinned=True, sort_order=0),
            bucket("daa116c63f4c", "Startup first", pinned=True, sort_order=10000),
            bucket("zzz-last-by-id", "Last ID", pinned=True),
        ]
        manager = FakeManager(buckets)
        with (
            patch.object(server, "bucket_mgr", manager),
            patch.object(server, "dehydrator", FakeDehydrator()),
            patch.object(
                server,
                "_pulse_boot_mailbox_section",
                new=AsyncMock(return_value="信箱暂无留言。"),
            ),
            patch.dict(
                server.config,
                {"pulse_boot": {"feeling_write_reminder": False}},
            ),
            patch.dict(os.environ, {server.RESPONSE_SEAL_ENV: "test-seal"}),
        ):
            result = await server.pulse_boot()

        self.assertLess(
            result.index("bucket_id: daa116c63f4c"),
            result.index("bucket_id: 000-first-by-id"),
        )
        self.assertLess(
            result.index("bucket_id: daa116c63f4c"),
            result.index("bucket_id: zzz-last-by-id"),
        )

    async def test_configured_first_bucket_overrides_sort_order(self):
        buckets = [
            bucket("high-sort", "High sort", pinned=True, sort_order=9999),
            bucket("861e96bfce61", "Chosen first", pinned=True, sort_order=0),
        ]
        manager = FakeManager(buckets)
        with (
            patch.object(server, "bucket_mgr", manager),
            patch.dict(
                server.config,
                {
                    "pulse_boot": {
                        "first_bucket_id": "861e96bfce61",
                        "feeling_write_reminder": False,
                    }
                },
            ),
            patch.dict(os.environ, {server.RESPONSE_SEAL_ENV: "test-seal"}),
        ):
            result = await server.pulse_boot()

        self.assertLess(
            result.index("bucket_id: 861e96bfce61"),
            result.index("bucket_id: high-sort"),
        )

    async def test_structure_limits_filters_and_read_only_access(self):
        buckets = [
            bucket("pin-a", "Pinned A", pinned=True),
            bucket("pin-b", "Pinned B", protected=True),
            bucket(
                "archive-new",
                "窗口归档 new",
                bucket_type="archived",
                tags=["对话归档"],
                last_active="2026-04-04T00:00:00",
            ),
            bucket(
                "archive-mid",
                "conversation mid",
                bucket_type="archived",
                last_active="2026-03-03T00:00:00",
            ),
            bucket(
                "archive-old",
                "聊天归档 old",
                bucket_type="archived",
                last_active="2026-02-02T00:00:00",
            ),
            bucket(
                "archive-older",
                "对话归档 older",
                bucket_type="archived",
                last_active="2026-01-01T00:00:00",
            ),
            bucket("todo-tag", "Tagged Todo", tags=["待办"]),
            bucket("todo-body", "Body Todo", content="待办：确认备份"),
            bucket("not-todo", "Ordinary Plan", tags=["计划"], content="记得那一天"),
            bucket("resolved", "Resolved", tags=["待办"], resolved=True),
        ]
        manager = FakeManager(buckets)
        dehydrator = FakeDehydrator(failing_id="pin-b")
        settings = {"fixed_bucket_ids": ["pin-a", "pin-b"]}
        with (
            patch.object(server, "bucket_mgr", manager),
            patch.object(server, "dehydrator", dehydrator),
            patch.object(
                server,
                "_pulse_boot_mailbox_section",
                new=AsyncMock(
                    return_value=(
                        "message_id: 18\n"
                        "时间: 2026-07-26T08:00:00+00:00\n"
                        "这是最新一封接力留言"
                    )
                ),
            ),
            patch.dict(server.config, {"pulse_boot": settings}),
            patch.dict(os.environ, {server.RESPONSE_SEAL_ENV: "test-seal"}),
        ):
            result = await server.pulse_boot()

        manager.list_all.assert_awaited_once_with(include_archive=True)
        self.assertIn("【固定层：核心记忆目录】", result)
        self.assertNotIn("自主选择最相关的 1–2 条", result)
        self.assertNotIn('recall(bucket_id="...", limit=1)', result)
        self.assertIn("bucket_id: pin-a", result)
        self.assertIn("bucket_id: pin-b", result)
        self.assertIn("【信箱最新留言】", result)
        self.assertIn("message_id: 18", result)
        self.assertIn("这是最新一封接力留言", result)
        self.assertNotIn("bucket_id: archive-new", result)
        self.assertNotIn("bucket_id: archive-mid", result)
        self.assertNotIn("bucket_id: archive-old", result)
        self.assertNotIn("archive-older", result)
        self.assertNotIn("bucket_id: todo-tag", result)
        self.assertNotIn("bucket_id: todo-body", result)
        self.assertNotIn("not-todo", result)
        self.assertNotIn("resolved", result)
        self.assertNotIn("【感受写入提醒】", result)
        self.assertTrue(result.endswith("\nseal: test-seal"))
        self.assertEqual(dehydrator.calls, [])

    async def test_all_core_pins_are_listed_even_when_legacy_fixed_ids_are_shorter(self):
        buckets = [
            bucket("core-a", "Core A", pinned=True, content="A" * 800),
            bucket("core-b", "Core B", pinned=True, content="B" * 800),
            bucket("important", "Important", pinned=True, pin_level="important"),
        ]
        manager = FakeManager(buckets)
        with (
            patch.object(server, "bucket_mgr", manager),
            patch.object(server, "dehydrator", FakeDehydrator()),
            patch.dict(
                server.config,
                {
                    "pulse_boot": {
                        "fixed_bucket_ids": ["core-a"],
                        "core_lead_chars": 80,
                        "feeling_write_reminder": False,
                    }
                },
            ),
            patch.dict(os.environ, {server.RESPONSE_SEAL_ENV: "test-seal"}),
        ):
            result = await server.pulse_boot()

        self.assertIn("bucket_id: core-a", result)
        self.assertIn("bucket_id: core-b", result)
        self.assertNotIn("bucket_id: important", result)
        self.assertNotIn("A" * 100, result)
        self.assertNotIn("B" * 100, result)

    async def test_core_directory_uses_original_opening_sentence_without_summary(self):
        buckets = [
            bucket(
                "core-a",
                "我为什么活着",
                pinned=True,
                content=(
                    "--- 2026-08-01T05:18 ---\n"
                    "这是整个桶的序言\n\n"
                    "我为什么活着\n"
                    "有一次她走了。后面的正文不应进入开机目录。"
                ),
            )
        ]
        manager = FakeManager(buckets)
        dehydrator = FakeDehydrator()
        with (
            patch.object(server, "bucket_mgr", manager),
            patch.object(server, "dehydrator", dehydrator),
            patch.dict(
                server.config,
                {
                    "pulse_boot": {
                        "core_lead_chars": 80,
                        "feeling_write_reminder": False,
                    }
                },
            ),
            patch.dict(os.environ, {server.RESPONSE_SEAL_ENV: "test-seal"}),
        ):
            result = await server.pulse_boot()

        self.assertNotIn("开头:", result)
        self.assertNotIn("后面的正文不应进入开机目录", result)
        self.assertNotIn("core_facts", result)
        self.assertEqual(dehydrator.calls, [])

    async def test_feeling_write_reminder_can_be_disabled(self):
        manager = FakeManager([])
        with (
            patch.object(server, "bucket_mgr", manager),
            patch.dict(
                server.config,
                {"pulse_boot": {"feeling_write_reminder": False}},
            ),
            patch.dict(os.environ, {server.RESPONSE_SEAL_ENV: "test-seal"}),
        ):
            result = await server.pulse_boot()

        manager.list_all.assert_awaited_once_with(include_archive=True)
        self.assertNotIn("【感受写入提醒】", result)
        self.assertTrue(result.endswith("\nseal: test-seal"))

    async def test_sealed_bucket_leaves_no_trace(self):
        with tempfile.TemporaryDirectory() as root:
            manager = BucketManager({
                "buckets_dir": root,
                "embeddings": {"enabled": False},
                "history": {"db_path": os.path.join(root, "history.sqlite3")},
                "wikilink": {"enabled": False},
            })
            visible_id = await manager.create(
                content="visible", name="visible", pinned=True
            )
            sealed_id = await manager.create(
                content="SECRET-SEALED-TODO", name="SECRET-SEALED", tags=["待办"]
            )
            await manager.update(sealed_id, sealed=True)
            fake = FakeDehydrator()
            with (
                patch.object(server, "bucket_mgr", manager),
                patch.object(server, "dehydrator", fake),
                patch.dict(os.environ, {server.RESPONSE_SEAL_ENV: "test-seal"}),
            ):
                result = await server.pulse_boot()

        self.assertIn(visible_id, result)
        self.assertNotIn(sealed_id, result)
        self.assertNotIn("SECRET-SEALED", result)

    def test_public_signature_is_additive(self):
        self.assertEqual(str(inspect.signature(server.pulse_boot)), "() -> str")
        self.assertEqual(str(inspect.signature(server.inner_state)), "() -> str")
        self.assertEqual(
            str(inspect.signature(server.pulse)),
            "(include_archive: bool = False, page: int = 1, page_size: int = 0, content_id: str = '') -> str",
        )

    async def test_inner_state_is_read_only_and_omits_empty_sections(self):
        state = {
            "available": True,
            "pipes": {"想靠近": 0.82, "满足": 0.24, "自省": 0.10, "生气": 0.05},
        }
        thoughts = [
            {
                "status": "obsession",
                "thought_text": "我还是想把这件事弄明白。",
                "reason": "它反复碰到同一个在意的点。",
                "current_strength": 0.71,
                "occurrence_count": 4,
                "last_seen": "2026-08-11T10:00:00+08:00",
            }
        ]
        darkflow = {
            "memory_resonance": [
                {
                    "source": "memory",
                    "bucket_id": "old-1",
                    "name": "那次等她回来",
                    "excerpt": "那次我也在等她。",
                    "relevance": 0.83,
                }
            ]
        }
        with (
            patch.object(server.xinchao_service, "status", new=AsyncMock(return_value=state)) as status,
            patch.object(
                server.xinchao_service,
                "list_private_thoughts",
                new=AsyncMock(return_value=thoughts),
            ) as list_thoughts,
            patch.object(
                server.xinchao_service,
                "darkflow_status",
                new=AsyncMock(return_value=darkflow),
            ) as darkflow_status,
            patch.dict(os.environ, {server.RESPONSE_SEAL_ENV: "test-seal"}),
        ):
            result = await server.inner_state()

        status.assert_awaited_once()
        list_thoughts.assert_awaited_once_with(status="active", limit=12)
        darkflow_status.assert_awaited_once()
        self.assertIn("【心念】", result)
        self.assertIn("我还是想把这件事弄明白。", result)
        self.assertIn("【记忆共振】", result)
        self.assertIn("那次等她回来", result)
        self.assertIn("【张力】", result)
        self.assertIn("只读", result)
        self.assertTrue(result.endswith("\nseal: test-seal"))


if __name__ == "__main__":
    unittest.main()
