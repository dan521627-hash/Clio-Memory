import inspect
import unittest
from unittest.mock import patch

import server


CONFLICT = {
    "old_bucket_id": "old-123",
    "old_bucket_name": "旧预算",
    "old_fact": "每月预算是3000元",
    "new_fact": "每月预算是5000元",
    "point": "同一预算金额不一致",
}


class FakeDecayEngine:
    async def ensure_started(self):
        return None


class FakeDetector:
    async def detect(self, _content):
        return [CONFLICT]


class FakeDehydrator:
    async def analyze(self, _content):
        return {
            "domain": ["财务"],
            "valence": 0.5,
            "arousal": 0.3,
            "tags": ["预算"],
            "suggested_name": "新预算",
        }


class FakeBucketManager:
    def __init__(self):
        self.search_calls = 0
        self.create_calls = 0

    async def search(self, *args, **kwargs):
        self.search_calls += 1
        raise AssertionError("conflicting writes must bypass merge search")

    async def create(self, **kwargs):
        self.create_calls += 1
        return "new-456"


class ServerConflictTests(unittest.IsolatedAsyncioTestCase):
    def test_public_tool_signatures_are_unchanged(self):
        self.assertEqual(
            str(inspect.signature(server.hold)),
            "(content: str, tags: str = '', importance: int = 5, pinned: bool = False, feeling: bool = False, trigger_date: str = '') -> str",
        )
        self.assertEqual(
            str(inspect.signature(server.grow)),
            "(content: str, message: str = '') -> str",
        )

    async def test_hold_keeps_conflict_as_separate_bucket_and_warns(self):
        manager = FakeBucketManager()
        with (
            patch.object(server, "bucket_mgr", manager),
            patch.object(server, "dehydrator", FakeDehydrator()),
            patch.object(server, "decay_engine", FakeDecayEngine()),
            patch.object(server, "conflict_detector", FakeDetector()),
        ):
            result = await server.hold("每月预算是5000元")

        self.assertEqual(manager.search_calls, 0)
        self.assertEqual(manager.create_calls, 1)
        self.assertIn("对账警告", result)
        self.assertIn("old-123", result)
        self.assertIn("3000元", result)
        self.assertIn("5000元", result)

    async def test_grow_keeps_whole_conflicting_input_in_one_bucket(self):
        manager = FakeBucketManager()
        with (
            patch.object(server, "bucket_mgr", manager),
            patch.object(server, "dehydrator", FakeDehydrator()),
            patch.object(server, "decay_engine", FakeDecayEngine()),
            patch.object(server, "conflict_detector", FakeDetector()),
        ):
            result = await server.grow("每月预算是5000元")

        self.assertEqual(manager.search_calls, 0)
        self.assertEqual(manager.create_calls, 1)
        self.assertIn("新建→new-456", result)
        self.assertIn("对账警告", result)
        self.assertIn("old-123", result)


if __name__ == "__main__":
    unittest.main()
