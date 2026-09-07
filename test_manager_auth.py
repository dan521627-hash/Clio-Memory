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

    def test_seen_button_keeps_push_for_ai_without_starting_chat_activity(self):
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
        settle = AsyncMock()
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
        settle.assert_not_awaited()
        purge_candidates.assert_not_awaited()

    def test_silence_ack_does_not_start_active_presence(self):
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
        observe = AsyncMock()
        cancel = AsyncMock()
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
                "observe_presence",
                new=observe,
            ),
            patch.object(
                manager_server.behavior_service.store,
                "cancel_for_activity",
                new=cancel,
            ),
        ):
            response = self.client.post(
                "/api/behavior/acknowledge", json={"action_id": 44}
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["phase"], "silence")
        acknowledge.assert_awaited_once_with(44)
        observe.assert_not_awaited()
        cancel.assert_not_awaited()
        purge.assert_not_awaited()

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

    def test_timeline_correction_reuses_one_reversible_effect_key(self):
        self.client.post(
            "/api/auth/login", json={"password": "test-mobile-password"}
        )
        record = AsyncMock(
            return_value={
                "status": "updated",
                "fact_key": "current_city",
                "version_id": 12,
            }
        )
        sidecar = AsyncMock(return_value={"status": "applied"})
        with (
            patch.object(manager_server.fact_timeline_store, "record", new=record),
            patch.object(manager_server, "_record_xinchao", new=sidecar),
        ):
            response = self.client.post(
                "/api/timeline",
                json={
                    "fact": "当前城市",
                    "value": "已经抵达北京",
                    "effective_date": "2026-08-23",
                },
            )

        self.assertEqual(response.status_code, 200)
        sidecar.assert_not_awaited()

    def test_timeline_api_survives_null_bucket_metadata(self):
        self.client.post(
            "/api/auth/login", json={"password": "test-mobile-password"}
        )
        list_facts = AsyncMock(
            return_value=[
                {
                    "fact_key": "current_city",
                    "fact_label": "当前城市",
                    "versions": [
                        {
                            "source_type": "bucket",
                            "source_bucket_id": "legacy-bucket",
                            "is_current": True,
                            "fact_value": "测试城市",
                        }
                    ],
                }
            ]
        )
        candidates = AsyncMock(return_value=[])
        bucket_get = AsyncMock(return_value={"metadata": None})
        with (
            patch.object(
                manager_server.fact_timeline_store, "list_facts", new=list_facts
            ),
            patch.object(
                manager_server.fact_timeline_store,
                "list_candidates",
                new=candidates,
            ),
            patch.object(manager_server.bucket_manager, "get", new=bucket_get),
        ):
            response = self.client.get("/api/timeline")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["count"], 1)


if __name__ == "__main__":
    unittest.main()
