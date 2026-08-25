import os
import shutil
import sqlite3
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from brain_context_service import build_brain_context
from xinchao_store import XinchaoService


class CorrectionEvaluator:
    prompt_hash = "correction-test"

    async def evaluate(self, content):
        delta = .45 if "错误" in content else .12
        return {
            "event": content, "event_tag": "同一件事", "context_card": content,
            "severity": .5, "pipes": {"想靠近": delta},
            "narrative_complete": True, "handoff_ready": True,
            "inner_thoughts": [], "quality_note": "",
        }


class ThoughtCorrectionEvaluator(CorrectionEvaluator):
    async def evaluate(self, content):
        result = await super().evaluate(content)
        result["event_tag"] = content
        tag = "修正后的念头" if "修正" in content else "反复出现的念头"
        result["inner_thoughts"] = [{
            "tag": tag,
            "text": tag,
            "tone": "mixed",
            "intensity": .4,
            "reason": content,
        }]
        return result


class BrainContextTests(unittest.IsolatedAsyncioTestCase):
    async def test_cross_layer_context_keeps_sources_separate(self):
        bucket_manager = SimpleNamespace(
            list_all=AsyncMock(return_value=[{
                "id": "memory-1",
                "metadata": {"name": "一段记忆", "created": "2026-08-23"},
                "content": "记忆原文",
            }]),
            embedding_index=None,
        )
        mailbox_store = SimpleNamespace(list=AsyncMock(return_value=[{
            "message_id": 7, "message": "信箱原文", "created_at": "2026-08-23"
        }]))
        xinchao_service = SimpleNamespace(
            list_private_thoughts=AsyncMock(return_value=[{
                "canonical_tag": "trace:7", "thought_kind": "trace",
                "thought_text": "念痕原文", "last_seen": "2026-08-23"
            }]),
            status=AsyncMock(return_value={"as_of": "2026-08-23", "pipes": {"想靠近": .4}}),
            disposition_preview=AsyncMock(return_value={"tendencies": []}),
        )
        task_service = SimpleNamespace(store=SimpleNamespace(list=AsyncMock(return_value=[{
            "task_id": 3, "title": "还没有完成", "details": "未竟原文",
            "status": "open", "created_at": "2026-08-23"
        }])))
        fact_store = SimpleNamespace(list_facts=AsyncMock(return_value=[{
            "fact_key": "location", "fact_label": "所在地点",
            "current": {"fact_value": "已经抵达", "effective_date": "2026-08-23"},
            "versions": [{"fact_value": "计划前往"}, {"fact_value": "已经抵达"}],
        }]))

        result = await build_brain_context(
            "", bucket_manager=bucket_manager, mailbox_store=mailbox_store,
            xinchao_service=xinchao_service, task_service=task_service,
            fact_timeline_store=fact_store, limit=6,
        )

        self.assertEqual(
            {item["source"] for item in result["nodes"]},
            {"memory", "mailbox", "trace", "task", "fact"},
        )
        self.assertEqual(result["write_policy"], "sources_remain_separate; derived effects are reversible")
        self.assertEqual(result["dimensions"]["Z"]["evidence_count"], 1)
        self.assertEqual(result["dimensions"]["E"]["evidence_count"], 2)
        self.assertIn("念痕原文", [item["text"] for item in result["nodes"]])

    async def test_corrected_write_supersedes_old_effect_before_new_effect(self):
        root = tempfile.mkdtemp(prefix="clio-correction-")
        db_path = os.path.join(root, "xinchao.sqlite3")
        try:
            service = XinchaoService({
                "buckets_dir": root,
                "dehydration": {"api_key": ""},
                "xinchao": {
                    "enabled": True, "db_path": db_path,
                    "monologue_enabled": False,
                    "exact_dedupe_hours": 24,
                },
            })
            service.evaluator = CorrectionEvaluator()
            first = await service.record_event(
                "这是错误写入", "manager_memory", "memory-1",
                correction_key="memory:memory-1",
            )
            second = await service.record_event(
                "这是修正后的写入", "manager_memory", "memory-1",
                correction_key="memory:memory-1",
            )
            state = await service.status()

            connection = sqlite3.connect(db_path)
            try:
                rows = connection.execute(
                    "SELECT status, deltas_json FROM xinchao_events ORDER BY event_id"
                ).fetchall()
            finally:
                connection.close()

            self.assertEqual(first["status"], "applied")
            self.assertEqual(second["status"], "applied")
            self.assertEqual(second["correction"]["status"], "rolled_back")
            self.assertEqual(rows[0][0], "superseded")
            self.assertEqual(rows[1][0], "applied")
            # The configured floor for 想靠近 is .18, so only the replacement
            # delta (.12) should remain. The old .45 must not accumulate.
            self.assertAlmostEqual(state["pipes"]["想靠近"], .30, places=2)
        finally:
            shutil.rmtree(root, ignore_errors=True)

    async def test_correction_removes_only_its_merged_thought_contribution(self):
        root = tempfile.mkdtemp(prefix="clio-thought-correction-")
        db_path = os.path.join(root, "xinchao.sqlite3")
        try:
            service = XinchaoService({
                "buckets_dir": root,
                "dehydration": {"api_key": ""},
                "xinchao": {
                    "enabled": True, "db_path": db_path,
                    "monologue_enabled": False,
                    "exact_dedupe_hours": 0,
                    "paraphrase_dedupe_seconds": 0,
                },
            })
            service.evaluator = ThoughtCorrectionEvaluator()
            await service.record_event(
                "第一条原写入", "manager_memory", "memory-1",
                correction_key="memory:memory-1",
            )
            await service.record_event(
                "第二条独立写入", "manager_memory", "memory-2",
                correction_key="memory:memory-2",
            )
            await service.record_event(
                "第一条修正写入", "manager_memory", "memory-1",
                correction_key="memory:memory-1",
            )

            connection = sqlite3.connect(db_path)
            connection.row_factory = sqlite3.Row
            try:
                shared = connection.execute(
                    "SELECT * FROM xinchao_thoughts WHERE canonical_tag=?",
                    (service._canonical_tag("反复出现的念头"),),
                ).fetchone()
                replacement = connection.execute(
                    "SELECT * FROM xinchao_thoughts WHERE canonical_tag=?",
                    (service._canonical_tag("修正后的念头"),),
                ).fetchone()
                active_contributions = connection.execute(
                    """
                    SELECT canonical_tag, COUNT(*) AS total
                    FROM xinchao_thought_contributions
                    WHERE reverted_at IS NULL GROUP BY canonical_tag
                    """
                ).fetchall()
            finally:
                connection.close()

            self.assertEqual(int(shared["occurrence_count"]), 1)
            self.assertEqual(str(shared["status"]), "flash")
            self.assertEqual(int(replacement["occurrence_count"]), 1)
            self.assertEqual(sorted(int(row["total"]) for row in active_contributions), [1, 1])
        finally:
            shutil.rmtree(root, ignore_errors=True)

    async def test_repeated_ai_traces_feed_disposition_without_merging_originals(self):
        root = tempfile.mkdtemp(prefix="clio-trace-disposition-")
        db_path = os.path.join(root, "xinchao.sqlite3")
        try:
            service = XinchaoService({
                "buckets_dir": root,
                "dehydration": {"api_key": ""},
                "xinchao": {"enabled": True, "db_path": db_path, "monologue_enabled": False},
            })
            first = await service.record_thought_trace(
                "第一次想把话继续下去", tag="愿意靠近",
                deltas={"想靠近": .08}, source_ref="window-1",
            )
            second = await service.record_thought_trace(
                "第二次仍想把话继续下去", tag="愿意靠近",
                deltas={"想靠近": .05}, source_ref="window-2",
            )
            report = await service.disposition_preview(30)

            self.assertNotEqual(
                first["thought"]["canonical_tag"], second["thought"]["canonical_tag"]
            )
            self.assertEqual(report["tendency"]["name"], "更愿意靠近")
            self.assertEqual(report["evidence_count"], 2)
            self.assertEqual(len(report["evidence"]), 2)
        finally:
            shutil.rmtree(root, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
