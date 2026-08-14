import tempfile
import unittest
from pathlib import Path

from task_service import TaskService


class DisabledEmbedding:
    enabled = False
    model_name = "disabled"


class DummyEvaluator:
    client = object()


class ScriptedTaskService(TaskService):
    def __init__(self, config, actions):
        super().__init__(config, DummyEvaluator(), DisabledEmbedding())
        self.actions = actions

    async def _extract(self, content, candidates):
        return self.actions


class TaskBoundaryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.config = {
            "buckets_dir": self.temp.name,
            "tasks": {
                "enabled": True,
                "auto_extract": True,
                "db_path": str(Path(self.temp.name) / "tasks.sqlite3"),
            },
        }

    async def asyncTearDown(self):
        self.temp.cleanup()

    async def test_relationship_attitude_is_not_a_task(self):
        service = ScriptedTaskService(self.config, [{
            "action": "create",
            "title": "以后对她好，不再说难听的话",
            "task_type": "ongoing_attitude",
            "completion_criterion": "以后一直做到",
        }])
        result = await service.process_event(
            "吵架后我说以后会对她好。", "mailbox", "1", "event:promise"
        )
        self.assertEqual(result["changes"], [])
        self.assertEqual((await service.store.counts())["total"], 0)

    async def test_finite_action_is_a_task(self):
        service = ScriptedTaskService(self.config, [{
            "action": "create",
            "title": "周五预约复诊",
            "task_type": "finite_action",
            "completion_criterion": "复诊时间已预约",
            "importance": 4,
        }])
        result = await service.process_event(
            "周五要预约复诊。", "mailbox", "2", "event:appointment"
        )
        self.assertEqual(result["changes"][0]["action"], "created")


if __name__ == "__main__":
    unittest.main()
