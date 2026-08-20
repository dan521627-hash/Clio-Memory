import unittest
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

import manager_server


class HousePhraseTests(unittest.TestCase):
    def setUp(self):
        manager_server.login_failures.clear()
        self.password_patch = patch.object(
            manager_server, "manager_password", "test-mobile-password"
        )
        self.password_patch.start()
        self.client = TestClient(manager_server.app)
        self.client.post(
            "/api/auth/login", json={"password": "test-mobile-password"}
        )

    def tearDown(self):
        self.client.close()
        self.password_patch.stop()

    def test_clean_house_phrase_removes_label_and_quotes(self):
        self.assertEqual(
            manager_server._clean_house_phrase(
                "题词：‘我把今天没说完的话，轻轻留在屋里。’"
            ),
            "我把今天没说完的话，轻轻留在屋里。",
        )

    def test_house_phrase_endpoint_returns_cached_text(self):
        generated = AsyncMock(
            return_value={
                "text": "我把今天没说完的话，轻轻留在屋里。",
                "generated_at": "2026-08-11T20:00:00+08:00",
                "generated": True,
            }
        )
        with patch.object(manager_server, "_get_house_phrase", new=generated):
            response = self.client.get("/api/house/phrase")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.json()["text"], "我把今天没说完的话，轻轻留在屋里。"
        )
        self.assertTrue(response.json()["generated"])
        generated.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
