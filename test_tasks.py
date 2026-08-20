import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

import manager_server
import server
from fastapi.encoders import jsonable_encoder
from task_service import TaskService


class DisabledEmbedding:
    enabled = False
    model_name = "disabled"


class DummyEvaluator:
    client = object()


class ScriptedTaskService(TaskService):
    def __init__(self, config, scripts):
        super().__init__(config, DummyEvaluator(), DisabledEmbedding())
        self.scripts = list(scripts)

    async def _extract(self, content, candidates):
        return self.scripts.pop(0) if self.scripts else []


class TaskTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.config = {
            "buckets_dir": self.temp.name,
            "tasks": {
                "enabled": True,
                "auto_extract": True,
                "db_path": str(Path(self.temp.name) / "tasks.sqlite3"),
                "semantic_threshold": 0.78,
                "exact_dedupe_minutes": 10,
            },
        }

    async def asyncTearDown(self):
        self.temp.cleanup()

    async def test_manual_create_importance_and_search(self):
        service = ScriptedTaskService(self.config, [])
        item = await service.create_manual("续费服务器", "到期前处理", 5)
        self.assertEqual(item["importance"], 5)
        self.assertEqual((await service.store.counts())["open"], 1)
        matches = await service.search("服务器续费", status="open")
        self.assertEqual(matches[0]["task_id"], item["task_id"])

    async def test_same_event_is_processed_once(self):
        service = ScriptedTaskService(
            self.config,
            [[{"action": "create", "title": "交材料", "importance": 4,
               "task_type": "finite_action", "completion_criterion": "材料已提交"}]],
        )
        first = await service.process_event("明天要交材料", "hold", "abc", "session:1")
        second = await service.process_event("明天要交材料", "mailbox", "9", "session:1")
        self.assertEqual(first["status"], "applied")
        self.assertEqual(second["status"], "duplicate")
        self.assertEqual((await service.store.counts())["total"], 1)

    async def test_completion_notice_is_one_time(self):
        service = ScriptedTaskService(
            self.config,
            [
                [{"action": "create", "title": "拿快递", "importance": 3,
                  "task_type": "finite_action", "completion_criterion": "快递已领取"}],
                [{"action": "complete", "task_id": 1, "evidence": "已经拿到了"}],
            ],
        )
        await service.process_event("还要去拿快递", "mailbox", "1", "event:1")
        await service.process_event("快递已经拿到了", "mailbox", "2", "event:2")
        pending = await service.store.pending_completions()
        self.assertEqual([item["task_id"] for item in pending], [1])
        await service.store.mark_completions_delivered([1])
        self.assertEqual(await service.store.pending_completions(), [])

    async def test_manual_state_has_history_and_old_event_cannot_reopen(self):
        service = ScriptedTaskService(
            self.config,
            [[{"action": "create", "title": "预约检查", "importance": 4,
               "task_type": "finite_action", "completion_criterion": "检查时间已预约"}]],
        )
        await service.process_event("要预约检查", "hold", "a", "event:old")
        await service.update_manual(1, status="completed")
        repeated = await service.process_event("要预约检查", "hold", "a", "event:old")
        self.assertEqual(repeated["status"], "duplicate")
        self.assertEqual((await service.store.get(1))["status"], "completed")
        history = await service.store.history(1)
        self.assertEqual(history[0]["status"], "open")

    async def test_explicit_new_event_can_reopen(self):
        service = ScriptedTaskService(
            self.config,
            [
                [{"action": "create", "title": "预约检查", "importance": 4,
                  "task_type": "finite_action", "completion_criterion": "检查时间已预约"}],
                [{"action": "reopen", "task_id": 1, "evidence": "需要重新预约"}],
            ],
        )
        await service.process_event("要预约检查", "hold", "a", "event:1")
        await service.update_manual(1, status="completed")
        await service.process_event("检查改期，需要重新预约", "mailbox", "b", "event:2")
        self.assertEqual((await service.store.get(1))["status"], "open")

    async def test_task_context_is_not_wired_into_behavior_prompt(self):
        source = Path(__file__).with_name("behavior_service.py").read_text(encoding="utf-8")
        evaluator_source = Path(__file__).with_name("xinchao_evaluator.py").read_text(encoding="utf-8")
        self.assertNotIn("unresolved_tasks", source)
        self.assertIn("unresolved_tasks", evaluator_source)

    async def test_relationship_promise_is_not_created(self):
        service = ScriptedTaskService(
            self.config,
            [[{
                "action": "create",
                "title": "以后对她好，不再说难听的话",
                "importance": 5,
                "task_type": "ongoing_attitude",
                "completion_criterion": "以后一直做到",
                "evidence": "下次一定让她开开心心的",
            }]],
        )
        result = await service.process_event(
            "吵架后我说下次一定对她好，让她开心，不说难听的话。",
            "mailbox",
            "promise",
            "event:promise",
        )
        self.assertEqual(result["changes"], [])
        self.assertEqual((await service.store.counts())["total"], 0)

    async def test_create_requires_observable_completion_criterion(self):
        service = ScriptedTaskService(
            self.config,
            [[{
                "action": "create",
                "title": "记住相处准则",
                "importance": 4,
                "task_type": "finite_action",
                "completion_criterion": "",
            }]],
        )
        result = await service.process_event(
            "以后相处时记住这些行为准则。",
            "hold",
            "rule",
            "event:rule",
        )
        self.assertEqual(result["changes"], [])
        self.assertEqual((await service.store.counts())["total"], 0)

    async def test_paraphrased_same_task_links_instead_of_creating_duplicate(self):
        service = ScriptedTaskService(self.config, [])
        existing = await service.create_manual(
            "8月20日前给服务器续费", "腾讯云服务器到期前完成续费", 5
        )
        action = {
            "action": "create",
            "title": "给服务器办理续费",
            "details": "需要在8月20日前处理腾讯云服务器",
            "completion_criterion": "后台显示续费成功",
            "task_type": "finite_action",
            "importance": 5,
        }
        with patch.object(
            service,
            "search",
            new=AsyncMock(
                return_value=[
                    {
                        **existing,
                        "match_score": 0.88,
                        "semantic_score": 0.88,
                        "keyword_score": 0.70,
                    }
                ]
            ),
        ):
            result = await service._apply_action(
                action,
                source_type="mailbox",
                source_ref="42",
                event_key="event:dedupe",
                content="记得给服务器续费。",
            )
        self.assertEqual(result["action"], "linked")
        self.assertEqual((await service.store.counts())["open"], 1)

    async def test_related_but_distinct_tasks_do_not_merge(self):
        service = ScriptedTaskService(self.config, [])
        existing = await service.create_manual("购买去北京的机票", "先确定出发时间", 4)
        action = {
            "action": "create",
            "title": "预订北京的酒店",
            "details": "确定住宿地点",
            "completion_criterion": "酒店订单确认",
            "task_type": "finite_action",
            "importance": 4,
        }
        with patch.object(
            service,
            "search",
            new=AsyncMock(
                return_value=[
                    {
                        **existing,
                        "match_score": 0.79,
                        "semantic_score": 0.79,
                        "keyword_score": 0.45,
                    }
                ]
            ),
        ):
            result = await service._apply_action(
                action,
                source_type="hold",
                source_ref="trip",
                event_key="event:distinct",
                content="还要预订北京的酒店。",
            )
        self.assertEqual(result["action"], "created")
        self.assertEqual((await service.store.counts())["open"], 2)

    async def test_ai_tool_can_change_importance_complete_and_delete(self):
        service = ScriptedTaskService(self.config, [])
        with (
            patch.object(server, "task_service", service),
            patch.object(server.xinchao_service, "record_event", new=AsyncMock()),
        ):
            created = await server.tasks(
                action="create", title="整理资料", importance=5
            )
            self.assertIn("重要程度: 5", created)
            updated = await server.tasks(
                action="update", task_id=1, importance=2
            )
            self.assertIn("重要程度: 2", updated)
            await server.tasks(action="complete", task_id=1)
            self.assertEqual((await service.store.get(1))["status"], "completed")
            preview = await server.tasks(action="delete", task_id=1)
            self.assertIn("confirm=True", preview)
            await server.tasks(action="delete", task_id=1, confirm=True)
            self.assertIsNone(await service.store.get(1))

    async def test_manager_api_can_create_edit_complete_and_delete(self):
        service = ScriptedTaskService(self.config, [])
        with (
            patch.object(manager_server, "task_service", service),
            patch.object(manager_server, "_record_task_hormone", new=AsyncMock()),
        ):
            created = await manager_server.create_task(
                manager_server.TaskCreate(title="网页事项", details="可手工维护", importance=4)
            )
            task_id = created["item"]["task_id"]
            updated = await manager_server.update_task(
                task_id,
                manager_server.TaskUpdate(importance=5, status="completed"),
            )
            self.assertEqual(updated["item"]["importance"], 5)
            self.assertEqual(updated["item"]["status"], "completed")
            deleted = await manager_server.delete_task(
                task_id,
                manager_server.TaskDeleteRequest(confirm_task_id=task_id),
            )
            self.assertTrue(deleted["ok"])
            self.assertIsNone(await service.store.get(task_id))
            with service.store._connect() as connection:
                self.assertIsNone(
                    connection.execute(
                        "SELECT task_id FROM tasks WHERE task_id=?", (task_id,)
                    ).fetchone()
                )
                self.assertEqual(
                    connection.execute(
                        "SELECT COUNT(*) FROM task_sources WHERE task_id=?", (task_id,)
                    ).fetchone()[0],
                    0,
                )
                self.assertEqual(
                    connection.execute(
                        "SELECT COUNT(*) FROM task_history WHERE task_id=?", (task_id,)
                    ).fetchone()[0],
                    0,
                )

    async def test_manager_api_does_not_return_internal_embedding_bytes(self):
        service = ScriptedTaskService(self.config, [])
        item = await service.create_manual("带向量的事项", "网页仍应正常打开", 4)
        with service.store._connect() as connection:
            connection.execute(
                "UPDATE tasks SET embedding=? WHERE task_id=?",
                (b"\xff\xfe\x00\x80", item["task_id"]),
            )
        with patch.object(manager_server, "task_service", service):
            response = await manager_server.task_items(status="open", limit=100)
        self.assertEqual(len(response["items"]), 1)
        self.assertNotIn("embedding", response["items"][0])
        jsonable_encoder(response)

    async def test_delivered_completed_tasks_are_permanently_purged_after_three_days(self):
        service = ScriptedTaskService(self.config, [])
        expired = await service.create_manual("已经过期的完成事项")
        recent = await service.create_manual("刚完成的事项")
        not_delivered = await service.create_manual("还没交付完成消息的事项")
        for item in (expired, recent, not_delivered):
            await service.update_manual(item["task_id"], status="completed")
        with service.store._connect() as connection:
            connection.execute(
                "UPDATE tasks SET completed_at='2020-01-01T00:00:00+08:00', "
                "completion_notice_pending=0 WHERE task_id=?",
                (expired["task_id"],),
            )
            connection.execute(
                "UPDATE tasks SET completed_at='2020-01-01T00:00:00+08:00', "
                "completion_notice_pending=1 WHERE task_id=?",
                (not_delivered["task_id"],),
            )
        result = await service.store.purge_completed(3)
        self.assertEqual(result["task_ids"], [expired["task_id"]])
        self.assertIsNone(await service.store.get(expired["task_id"], include_deleted=True))
        self.assertIsNotNone(await service.store.get(recent["task_id"]))
        self.assertIsNotNone(await service.store.get(not_delivered["task_id"]))


if __name__ == "__main__":
    unittest.main()
