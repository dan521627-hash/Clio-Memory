import os
import tempfile
import unittest
from unittest.mock import patch

import manager_server
import server
from manager_server import (
    TreasuryCreate,
    TreasuryDeleteRequest,
    TreasuryUpdate,
)
from treasury_store import TreasuryStore


def config_for(root: str) -> dict:
    return {
        "buckets_dir": root,
        "treasury": {
            "db_path": os.path.join(root, "treasury.sqlite3"),
            "currency": "CNY",
            "symbol": "¥",
        },
    }


class TreasuryStoreTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = TreasuryStore(config_for(self.temp.name))

    async def asyncTearDown(self):
        self.temp.cleanup()

    async def test_income_expense_and_free_text_reason_calculate_totals(self):
        income = await self.store.record(
            "income", "100.50", "用户奖励我今天表现不错", source="test"
        )
        expense = await self.store.record(
            "expense", "28.30", "偷偷给她准备礼物", source="test"
        )
        summary = await self.store.summary()

        self.assertEqual(income["entry"]["reason"], "用户奖励我今天表现不错")
        self.assertEqual(expense["entry"]["reason"], "偷偷给她准备礼物")
        self.assertEqual(summary["total_income"], "100.50")
        self.assertEqual(summary["total_expense"], "28.30")
        self.assertEqual(summary["balance"], "72.20")
        self.assertEqual(summary["entry_count"], 2)

    async def test_reason_is_required_and_amount_is_exact_to_cents(self):
        with self.assertRaisesRegex(ValueError, "原因"):
            await self.store.record("income", "10", "   ")
        with self.assertRaisesRegex(ValueError, "两位小数"):
            await self.store.record("income", "1.001", "测试")

    async def test_update_and_delete_keep_complete_history(self):
        created = await self.store.record(
            "income", "100", "原始原因", occurred_at="2026-07-24T10:00"
        )
        entry_id = created["entry"]["entry_id"]

        updated = await self.store.update(
            entry_id,
            entry_type="expense",
            amount="25.50",
            reason="修改后的自由原因",
            occurred_at="2026-07-24T11:00",
        )
        deleted = await self.store.delete(entry_id)
        history = await self.store.history(entry_id)

        self.assertEqual(updated["entry"]["entry_type"], "expense")
        self.assertEqual(updated["entry"]["amount"], "25.50")
        self.assertEqual(deleted["summary"]["entry_count"], 0)
        self.assertEqual([item["operation"] for item in history], ["delete", "update"])
        self.assertEqual(history[0]["reason"], "修改后的自由原因")
        self.assertEqual(history[1]["reason"], "原始原因")
        self.assertEqual(history[1]["amount"], "100.00")


class TreasuryToolTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = TreasuryStore(config_for(self.temp.name))
        self.store_patch = patch.object(server, "treasury_store", self.store)
        self.store_patch.start()
        self.seal_patch = patch.dict(
            os.environ, {server.RESPONSE_SEAL_ENV: "treasury-test-seal"}
        )
        self.seal_patch.start()

    async def asyncTearDown(self):
        self.seal_patch.stop()
        self.store_patch.stop()
        self.temp.cleanup()

    async def test_tool_records_and_always_returns_three_totals(self):
        income = await server.treasury(
            action="income",
            amount=100,
            reason="用户发的工资",
        )
        expense = await server.treasury(
            action="expense",
            amount=20,
            reason="给用户买礼物",
        )
        status = await server.treasury(action="status")

        for response in (income, expense, status):
            self.assertIn("当前总金额", response)
            self.assertIn("累计总收入", response)
            self.assertIn("累计总支出", response)
            self.assertIn("seal: treasury-test-seal", response)
        self.assertIn("¥80.00", status)
        self.assertIn("用户发的工资", status)

    async def test_tool_update_delete_require_confirmation(self):
        await server.treasury(action="income", amount=50, reason="第一笔")
        preview = await server.treasury(
            action="update", entry_id=1, reason="改过的原因"
        )
        self.assertIn("尚未修改", preview)
        current = await self.store.get(1)
        self.assertEqual(current["reason"], "第一笔")

        changed = await server.treasury(
            action="update",
            entry_id=1,
            reason="改过的原因",
            confirm=True,
        )
        self.assertIn("已修改", changed)
        delete_preview = await server.treasury(action="delete", entry_id=1)
        self.assertIn("尚未删除", delete_preview)
        deleted = await server.treasury(
            action="delete", entry_id=1, confirm=True
        )
        self.assertIn("已删除", deleted)


class TreasuryManagerApiTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = TreasuryStore(config_for(self.temp.name))
        self.patch = patch.object(manager_server, "treasury_store", self.store)
        self.patch.start()

    async def asyncTearDown(self):
        self.patch.stop()
        self.temp.cleanup()

    async def test_manager_create_update_delete_and_history(self):
        created = await manager_server.create_treasury_entry(
            TreasuryCreate(
                entry_type="income",
                amount="88.00",
                reason="页面自由填写的原因",
                occurred_at="2026-07-24T12:00",
            )
        )
        entry_id = created["entry"]["entry_id"]
        self.assertEqual(created["summary"]["balance"], "88.00")

        updated = await manager_server.update_treasury_entry(
            entry_id,
            TreasuryUpdate(amount="80", reason="页面修改后的原因"),
        )
        self.assertEqual(updated["entry"]["reason"], "页面修改后的原因")
        history = await manager_server.treasury_entry_history(entry_id, limit=20)
        self.assertEqual(len(history["items"]), 1)

        deleted = await manager_server.delete_treasury_entry(
            entry_id, TreasuryDeleteRequest(confirm_entry_id=entry_id)
        )
        self.assertTrue(deleted["snapshot_created"])
        history = await manager_server.treasury_entry_history(entry_id, limit=20)
        self.assertEqual(len(history["items"]), 2)


if __name__ == "__main__":
    unittest.main()
