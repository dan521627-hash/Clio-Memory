import os
import unittest
from unittest.mock import AsyncMock, patch

import server


class SelfStateTests(unittest.IsolatedAsyncioTestCase):
    async def test_ai_can_read_current_state_and_personality_trajectory_on_demand(self):
        state = {
            "available": True,
            "pipes": {"想靠近": 0.62, "想分享": 0.31},
            "dominant": "想靠近",
            "dominant_value": 0.62,
        }
        thoughts = [
            {
                "status": "flash",
                "thought_text": "我想知道她现在在做什么。",
                "current_strength": 0.58,
            }
        ]
        trajectory = {
            "patterns": [
                {
                    "pattern": "担心她的安全",
                    "evidence_count": 3,
                    "last_seen": "2026-08-20T08:00:00+08:00",
                }
            ],
            "recurring_thoughts": [],
        }
        with (
            patch.object(server.xinchao_service, "status", new=AsyncMock(return_value=state)),
            patch.object(
                server.xinchao_service,
                "list_private_thoughts",
                new=AsyncMock(return_value=thoughts),
            ),
            patch.object(
                server.xinchao_service,
                "personality_preview",
                new=AsyncMock(return_value=trajectory),
            ),
            patch.object(
                server.xinchao_service,
                "render_full",
                return_value="主导状态: 想靠近 0.62",
            ),
            patch.dict(os.environ, {server.RESPONSE_SEAL_ENV: "test-seal"}),
        ):
            result = await server.self_state(days=30)

        self.assertIn("【此刻】", result)
        self.assertIn("想靠近 0.62", result)
        self.assertIn("【心念】", result)
        self.assertIn("【近 30 天的人格轨迹】", result)
        self.assertIn("担心她的安全", result)
        self.assertIn("不会自动改写人格设定", result)
        self.assertIn("seal: test-seal", result)


if __name__ == "__main__":
    unittest.main()
