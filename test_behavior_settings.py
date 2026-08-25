import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

import manager_server


class ManagerBehaviorSettingsTests(unittest.TestCase):
    def setUp(self):
        manager_server.login_failures.clear()
        self.password_patch = patch.object(
            manager_server, "manager_password", "settings-test-password"
        )
        self.password_patch.start()
        self.temp = tempfile.TemporaryDirectory()
        self.auth_path_patch = patch.object(
            manager_server,
            "manager_auth_path",
            Path(self.temp.name) / ".clio-manager-auth.json",
        )
        self.auth_path_patch.start()
        self.client = TestClient(manager_server.app)
        response = self.client.post(
            "/api/auth/login", json={"password": "settings-test-password"}
        )
        self.assertEqual(response.status_code, 200)

    def tearDown(self):
        self.client.close()
        self.auth_path_patch.stop()
        self.password_patch.stop()
        self.temp.cleanup()

    def test_push_title_round_trip(self):
        with (
            patch.object(
                manager_server.behavior_service,
                "push_title",
                new=AsyncMock(side_effect=["Clio", "沈野"]),
            ),
            patch.object(
                manager_server.behavior_service,
                "set_push_title",
                new=AsyncMock(return_value="沈野"),
            ) as setter,
        ):
            before = self.client.get("/api/behavior/settings")
            updated = self.client.put(
                "/api/behavior/settings", json={"push_title": "沈野"}
            )

        self.assertEqual(before.status_code, 200)
        self.assertEqual(before.json()["push_title"], "Clio")
        self.assertEqual(updated.status_code, 200)
        self.assertEqual(updated.json()["push_title"], "沈野")
        setter.assert_awaited_once_with("沈野")

    def test_password_change_rotates_cookie_and_rejects_old_password(self):
        changed = self.client.post(
            "/api/auth/change-password",
            json={
                "current_password": "settings-test-password",
                "new_password": "new-local-password",
                "confirm_password": "new-local-password",
            },
        )
        self.assertEqual(changed.status_code, 200)
        self.assertTrue(manager_server.manager_auth_path.exists())
        self.client.post("/api/auth/logout")
        old_login = self.client.post(
            "/api/auth/login", json={"password": "settings-test-password"}
        )
        new_login = self.client.post(
            "/api/auth/login", json={"password": "new-local-password"}
        )
        self.assertEqual(old_login.status_code, 401)
        self.assertEqual(new_login.status_code, 200)


if __name__ == "__main__":
    unittest.main()
