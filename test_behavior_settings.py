import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from behavior_service import BehaviorService


class BehaviorSettingsTests(unittest.IsolatedAsyncioTestCase):
    def _service(self, root: Path) -> BehaviorService:
        config = {
            "buckets_dir": str(root),
            "behavior": {
                "db_path": str(root / "behavior.sqlite3"),
                "title": "Clio",
            },
        }
        return BehaviorService(config, evaluator=None)

    async def test_push_title_persists_and_is_used_by_bark(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            service = self._service(root)
            self.assertEqual(await service.push_title(), "Clio")
            self.assertEqual(await service.set_push_title("顾川"), "顾川")

            restarted = self._service(root)
            self.assertEqual(await restarted.push_title(), "顾川")
            await restarted.set_push_title("新名字")

            response = AsyncMock()
            response.raise_for_status = lambda: None
            client = AsyncMock()
            client.post.return_value = response
            context = AsyncMock()
            context.__aenter__.return_value = client
            with patch("behavior_service.httpx.AsyncClient", return_value=context):
                restarted.device_key = "test-device"
                await restarted._send_bark("测试消息")

            payload = client.post.await_args.kwargs["json"]
            self.assertEqual(payload["title"], "新名字")


if __name__ == "__main__":
    unittest.main()
