import unittest
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

import manager_server


class ManagerAuthenticationTests(unittest.TestCase):
    def setUp(self):
        manager_server.login_failures.clear()
        self.password_patch = patch.object(
            manager_server, "manager_password", "test-mobile-password"
        )
        self.password_patch.start()
        self.client = TestClient(manager_server.app)

    def tearDown(self):
        self.client.close()
        self.password_patch.stop()

    def test_protected_api_requires_login(self):
        response = self.client.get("/api/health")
        self.assertEqual(response.status_code, 401)

    def test_login_sets_secure_session_for_followup_requests(self):
        rejected = self.client.post(
            "/api/auth/login", json={"password": "incorrect"}
        )
        self.assertEqual(rejected.status_code, 401)

        accepted = self.client.post(
            "/api/auth/login", json={"password": "test-mobile-password"}
        )
        self.assertEqual(accepted.status_code, 200)
        status = self.client.get("/api/auth/status").json()
        self.assertTrue(status["authenticated"])
        self.assertEqual(self.client.get("/api/health").status_code, 200)

    def test_seen_button_keeps_push_for_ai_and_restarts_silence(self):
        self.client.post(
            "/api/auth/login", json={"password": "test-mobile-password"}
        )
        acknowledge = AsyncMock(
            return_value={
                "status": "acknowledged",
                "count": 1,
                "cycle_ids": [8],
                "stateful_cycle_ids": [8],
                "stateful_action_ids": [9],
                "silence_action_ids": [],
                "phase": "absence",
                "acknowledged_at": "2026-08-11T20:00:00+08:00",
            }
        )
        settle = AsyncMock(
            return_value={"silence_started_at": "2026-08-11T20:00:00+08:00"}
        )
        purge_candidates = AsyncMock(return_value=1)
        with (
            patch.object(
                manager_server.behavior_service.store,
                "acknowledge_pending",
                new=acknowledge,
            ),
            patch.object(
                manager_server.behavior_service.store,
                "purge_cycle_candidates",
                new=purge_candidates,
            ),
            patch.object(
                manager_server.xinchao_service,
                "acknowledge_seen",
                new=settle,
            ),
        ):
            response = self.client.post("/api/behavior/acknowledge")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "acknowledged")
        acknowledge.assert_awaited_once()
        settle.assert_awaited_once()
        purge_candidates.assert_awaited_once_with([8])

    def test_legacy_silence_ack_deletes_plain_nudge_without_restarting_timer(self):
        self.client.post(
            "/api/auth/login", json={"password": "test-mobile-password"}
        )
        acknowledge = AsyncMock(
            return_value={
                "status": "acknowledged",
                "count": 1,
                "cycle_ids": [12],
                "stateful_cycle_ids": [],
                "stateful_action_ids": [],
                "silence_action_ids": [44],
                "phase": "silence",
                "acknowledged_at": "2026-08-11T20:00:00+08:00",
            }
        )
        restart = AsyncMock(
            return_value={"silence_started_at": "2026-08-11T20:00:00+08:00"}
        )
        settle = AsyncMock()
        purge = AsyncMock(return_value=1)
        with (
            patch.object(
                manager_server.behavior_service.store,
                "acknowledge_pending",
                new=acknowledge,
            ),
            patch.object(
                manager_server.behavior_service.store,
                "purge_handoff",
                new=purge,
            ),
            patch.object(
                manager_server.xinchao_service,
                "restart_silence_timer",
                new=restart,
            ),
            patch.object(
                manager_server.xinchao_service,
                "acknowledge_seen",
                new=settle,
            ),
        ):
            response = self.client.post(
                "/api/behavior/acknowledge", json={"action_id": 44}
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["phase"], "legacy_silence")
        acknowledge.assert_awaited_once_with(44)
        restart.assert_not_awaited()
        settle.assert_not_awaited()
        purge.assert_awaited_once_with([44])

    def test_calendar_endpoint_uses_all_read_only_sources(self):
        self.client.post(
            "/api/auth/login", json={"password": "test-mobile-password"}
        )
        bucket_list = AsyncMock(return_value=[])
        mailbox_list = AsyncMock(return_value=[])
        behavior_list = AsyncMock(return_value=[])
        task_list = AsyncMock(return_value=[])
        treasury_list = AsyncMock(return_value=[])
        thought_list = AsyncMock(return_value=[])
        darkflow_status = AsyncMock(return_value=None)
        fact_list = AsyncMock(return_value=[])
        fact_candidates = AsyncMock(return_value=[])
        with (
            patch.object(manager_server.bucket_manager, "list_all", new=bucket_list),
            patch.object(
                manager_server.mailbox_store, "search_pool", new=mailbox_list
            ),
            patch.object(
                manager_server.behavior_service.store, "list", new=behavior_list
            ),
            patch.object(manager_server.task_service.store, "list", new=task_list),
            patch.object(manager_server.treasury_store, "list", new=treasury_list),
            patch.object(
                manager_server.xinchao_service,
                "list_private_thoughts",
                new=thought_list,
            ),
            patch.object(
                manager_server.xinchao_service,
                "darkflow_status",
                new=darkflow_status,
            ),
            patch.object(
                manager_server.fact_timeline_store, "list_facts", new=fact_list
            ),
            patch.object(
                manager_server.fact_timeline_store,
                "list_candidates",
                new=fact_candidates,
            ),
        ):
            response = self.client.get("/api/calendar?date=2026-08-11")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"date": "2026-08-11", "items": [], "count": 0})
        bucket_list.assert_awaited_once_with(include_archive=True, include_sealed=True)
        mailbox_list.assert_awaited_once_with(include_deleted=False, limit=5000)
        thought_list.assert_awaited_once_with(status="all", limit=500)
        darkflow_status.assert_awaited_once()
        fact_list.assert_awaited_once_with(limit=200)

    def test_timeline_api_returns_pending_candidates(self):
        self.client.post(
            "/api/auth/login", json={"password": "test-mobile-password"}
        )
        list_facts = AsyncMock(return_value=[])
        candidates = AsyncMock(
            return_value=[
                {
                    "candidate_id": 7,
                    "fact_label": "续费日期",
                    "proposed_value": "2026-08-20",
                    "status": "pending",
                }
            ]
        )
        with (
            patch.object(
                manager_server.fact_timeline_store, "list_facts", new=list_facts
            ),
            patch.object(
                manager_server.fact_timeline_store,
                "list_candidates",
                new=candidates,
            ),
        ):
            response = self.client.get("/api/timeline")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["candidate_count"], 1)
        self.assertEqual(response.json()["candidates"][0]["candidate_id"], 7)


if __name__ == "__main__":
    unittest.main()
