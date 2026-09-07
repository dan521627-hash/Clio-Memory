import unittest
import os
import tempfile
from datetime import datetime

from memory_upgrade import (
    canonical_tendency,
    canonical_topic,
    expanded_query,
    mailbox_continuity,
    query_time_window,
    retrieval_reason,
    self_awareness,
)
from topic_store import TOPIC_TREE, TopicStore, extract_people, suggest_topic


class MemoryUpgradeTests(unittest.TestCase):
    def test_aliases_converge_without_changing_unknown_names(self):
        self.assertEqual(canonical_topic("上班焦虑"), "工作压力")
        self.assertEqual(canonical_tendency("主动靠近"), "更愿意靠近")
        self.assertEqual(canonical_topic("她自己的主题"), "她自己的主题")

    def test_query_keeps_original_and_adds_colloquial_synonyms(self):
        result = expanded_query("我上班时不舒服")
        self.assertTrue(result.startswith("我上班时不舒服"))
        self.assertIn("工作", result)
        self.assertIn("身体不适", result)

    def test_relative_and_explicit_time_windows(self):
        now = datetime(2026, 9, 7, 10, 20)
        start, end = query_time_window("昨天发生了什么", now)
        self.assertEqual(start.isoformat(), "2026-09-06T00:00:00")
        self.assertEqual(end.isoformat(), "2026-09-07T00:00:00")
        start, end = query_time_window("2026年8月的决定", now)
        self.assertEqual(start.isoformat(), "2026-08-01T00:00:00")
        self.assertEqual(end.isoformat(), "2026-09-01T00:00:00")

    def test_six_mailbox_events_are_chronological_and_status_labeled(self):
        messages = [
            {"message_id": i, "created_at": f"2026-09-0{i}T10:00:00", "message": text}
            for i, text in enumerate(
                ["还在讨论颜色", "决定使用蓝色", "这个方案不要", "继续检查", "可以做搜索", "等待结果", "更旧一封"],
                start=1,
            )
        ][::-1]
        result = mailbox_continuity(messages)
        self.assertEqual(result.count("（"), 6)
        self.assertNotIn("还在讨论颜色", result)
        self.assertIn("已决定", result)
        self.assertIn("已否决", result)
        self.assertFalse(result.startswith("- "))
        self.assertIn("最初", result)

    def test_mailbox_summary_deduplicates_same_event_but_keeps_change(self):
        messages = [
            {"created_at": "3", "message": "后来颜色改成绿色，这就是最后决定。"},
            {"created_at": "2", "message": "决定使用蓝色。"},
            {"created_at": "1", "message": "嗯，然后我觉得决定使用蓝色。"},
        ]
        result = mailbox_continuity(messages)
        self.assertEqual(result.count("决定使用蓝色"), 1)
        self.assertIn("改成绿色", result)
        self.assertLess(result.index("决定使用蓝色"), result.index("改成绿色"))

    def test_self_awareness_uses_existing_trajectory(self):
        result = self_awareness({
            "composite": {"summary": "目前主要呈现“更愿意靠近”。"},
            "observing": [{"label": "爱复盘"}],
        })
        self.assertIn("更愿意靠近", result)
        self.assertIn("更习惯复盘后再行动", result)

    def test_people_topic_exists_and_new_family_memory_is_suggested(self):
        self.assertIn("人物关系", TOPIC_TREE)
        result = suggest_topic("妈妈", "我妈今天来找我聊天")
        self.assertEqual(result["main_topic"], "人物关系")
        self.assertEqual(result["subtopic"], "家庭关系")

    def test_people_aliases_share_one_person_link_and_remove_together(self):
        with tempfile.TemporaryDirectory() as root:
            store = TopicStore({"buckets_dir": root, "topics": {"db_path": os.path.join(root, "topics.sqlite3")}})
            result = store._auto_assign_sync("new-1", "今天", "我妈和妈妈一起吃饭")
            self.assertEqual(result["people"], [{"person": "妈妈", "aliases": ["妈妈", "我妈"]}])
            self.assertEqual(store._get_sync("new-1")["main_topic"], "人物关系")
            self.assertEqual(len(__import__("asyncio").run(store.people_for("new-1"))), 1)
            self.assertTrue(store._remove_sync("new-1"))
            self.assertEqual(__import__("asyncio").run(store.people_for("new-1")), [])

    def test_people_extraction_filters_common_words(self):
        self.assertEqual(extract_people("系统文件", "今天系统和文件一起更新"), [])

    def test_retrieval_reason_is_short_and_source_based(self):
        reason = retrieval_reason("妈妈", {"metadata": {"name": "妈妈", "domain": ["人物关系"]}})
        self.assertIn("人物相同", reason)
        self.assertLess(len(reason), 80)


if __name__ == "__main__":
    unittest.main()
