import os
import tempfile
import unittest
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, patch

from behavior_service import BehaviorService
from utils import beijing_now


class FakeBehaviorEvaluator:
    def __init__(self):
        self.calls = []
        self.schedule_calls = []

    async def behavior_schedule(self, **context):
        self.schedule_calls.append(context)
        return {"follow_up": True, "delay_minutes": 10, "reason": "等她到地方"}

    async def behavior_decision(self, **context):
        self.calls.append(context)
        return {"action_type": "message", "content": "到地方了吗？到了跟我说一声。"}


class SkippingBehaviorEvaluator(FakeBehaviorEvaluator):
    async def behavior_schedule(self, **context):
        self.schedule_calls.append(context)
        return {
            "follow_up": False,
            "delay_minutes": 30,
            "reason": "event considered naturally complete",
        }

    async def behavior_decision(self, **context):
        self.calls.append(context)
        return {"action_type": "skip", "content": "", "reason": "complete"}


class LongingBehaviorEvaluator(FakeBehaviorEvaluator):
    async def behavior_decision(self, **context):
        self.calls.append(context)
        return {
            "action_type": "message",
            "content": "我想你了。",
            "reason": "暗涌和激素共同表现为想念",
        }


class StrongLongingBehaviorEvaluator(FakeBehaviorEvaluator):
    async def behavior_decision(self, **context):
        self.calls.append(context)
        return {
            "action_type": "message",
            "messages": ["啧。", "突然特别想你。", "想得有点烦。"],
            "aftereffect": {"想靠近": 0.04, "想分享": -0.02},
            "reason": "强烈想念形成连续表达",
        }


class VerboseBehaviorEvaluator(FakeBehaviorEvaluator):
    async def behavior_decision(self, **context):
        self.calls.append(context)
        return {"action_type": "message", "content": "长" * 160}


class RegeneratingBehaviorEvaluator(FakeBehaviorEvaluator):
    async def behavior_decision(self, **context):
        self.calls.append(context)
        if context.get("retry_instruction"):
            return {"action_type": "message", "content": "回来时给我带一声笑。"}
        return {"action_type": "message", "content": "我想你了。"}


class PerspectiveRepairEvaluator(FakeBehaviorEvaluator):
    async def behavior_decision(self, **context):
        self.calls.append(context)
        if context.get("retry_instruction"):
            return {"action_type": "message", "content": "你出去这么久，我一直在等你。"}
        return {"action_type": "message", "content": "她出去很久了，他一直在等她。"}


class StubbornThirdPersonEvaluator(FakeBehaviorEvaluator):
    async def behavior_decision(self, **context):
        self.calls.append(context)
        return {"action_type": "message", "content": "她还没回来，他还在等她。"}


def config(root, mode="rehearsal"):
    return {
        "buckets_dir": root,
        "behavior": {
            "db_path": os.path.join(root, "behavior.sqlite3"),
            "enabled": True,
            "mode": mode,
            "max_chars": 80,
        },
    }


class BehaviorServiceTests(unittest.IsolatedAsyncioTestCase):
    async def test_beijing_night_quiet_does_not_generate_or_record_a_push(self):
        with tempfile.TemporaryDirectory() as root:
            settings = config(root)
            settings["behavior"].update({"quiet_start": "00:00", "quiet_end": "07:00"})
            evaluator = LongingBehaviorEvaluator()
            service = BehaviorService(settings, evaluator)
            night = datetime.fromisoformat("2026-08-10T02:30:00+08:00")
            with patch("behavior_service.beijing_now", return_value=night):
                result = await service.process(
                    {"cycle_id": 88, "stage_index": 2, "content": "我很想她。"},
                    {"pipes": {"想靠近": 0.8}, "dominant": "想靠近"},
                    None,
                )
            actions = await service.store.list()

        self.assertEqual(result["status"], "quiet")
        self.assertEqual(actions, [])
        self.assertEqual(evaluator.calls, [])

    async def test_third_person_push_is_rewritten_before_delivery(self):
        with tempfile.TemporaryDirectory() as root:
            evaluator = PerspectiveRepairEvaluator()
            service = BehaviorService(config(root), evaluator)
            result = await service.process(
                {"cycle_id": 80, "stage_index": 1, "content": "她出门了。", "contexts": []},
                {"pipes": {"想靠近": 0.7}, "dominant": "想靠近"},
                None,
            )

        self.assertEqual(result["status"], "rehearsal")
        self.assertEqual(result["item"]["content"], "你出去这么久，我一直在等你。")
        self.assertEqual(len(evaluator.calls), 2)
        self.assertIn("第一人称", evaluator.calls[1]["retry_instruction"])

    async def test_unrepaired_third_person_push_is_not_sent(self):
        with tempfile.TemporaryDirectory() as root:
            evaluator = StubbornThirdPersonEvaluator()
            service = BehaviorService(config(root), evaluator)
            result = await service.process(
                {"cycle_id": 81, "stage_index": 1, "content": "她出门了。", "contexts": []},
                {"pipes": {"想靠近": 0.7}, "dominant": "想靠近"},
                None,
            )
            actions = await service.store.list()

        self.assertEqual(result["status"], "skipped")
        self.assertEqual(len(actions), 1)
        self.assertEqual(actions[0]["status"], "skipped")
        self.assertEqual(actions[0]["content"], "")
        self.assertEqual(len(evaluator.calls), 2)

    async def test_each_spoken_message_is_hard_limited(self):
        with tempfile.TemporaryDirectory() as root:
            service = BehaviorService(config(root), VerboseBehaviorEvaluator())
            await service.process(
                {"cycle_id": 24, "stage_index": 1, "content": "一段很长的暗涌", "contexts": []},
                {"pipes": {"想靠近": 0.5}, "dominant": "想靠近"},
                None,
            )
            actions = await service.store.list()

        self.assertEqual(len(actions[0]["content"]), 80)

    async def test_cycle_handoff_lists_only_successfully_sent_actions(self):
        with tempfile.TemporaryDirectory() as root:
            service = BehaviorService(config(root), FakeBehaviorEvaluator())
            await service.store.record(
                {"cycle_id": 8, "stage_index": 1, "content": "已发送", "status": "sent"}
            )
            await service.store.record(
                {"cycle_id": 8, "stage_index": 2, "content": "仅演习", "status": "rehearsal"}
            )
            await service.store.record(
                {"cycle_id": 9, "stage_index": 1, "content": "其他轮次", "status": "sent"}
            )
            actions = await service.store.list_sent_for_cycle(8)

        self.assertEqual([item["content"] for item in actions], ["已发送"])

    async def test_strong_darkflow_can_keep_three_voice_shaped_messages(self):
        with tempfile.TemporaryDirectory() as root:
            service = BehaviorService(config(root), StrongLongingBehaviorEvaluator())
            result = await service.process(
                {"cycle_id": 22, "stage_index": 1, "content": "想她想得烦。", "contexts": []},
                {"pipes": {"想靠近": 0.86, "想黏着": 0.81}, "dominant": "想靠近"},
                None,
            )
            actions = await service.store.list()

        self.assertEqual(result["status"], "rehearsal")
        self.assertEqual(actions[0]["context"]["message_count"], 3)
        self.assertEqual(actions[0]["content"], "啧。\n---\n突然特别想你。\n---\n想得有点烦。")

    async def test_normal_intensity_limits_a_model_burst_to_one_message(self):
        with tempfile.TemporaryDirectory() as root:
            service = BehaviorService(config(root), StrongLongingBehaviorEvaluator())
            await service.process(
                {"cycle_id": 23, "stage_index": 1, "content": "有点想她。", "contexts": []},
                {"pipes": {"想靠近": 0.55, "想黏着": 0.42}, "dominant": "想靠近"},
                None,
            )
            actions = await service.store.list()

        self.assertEqual(actions[0]["context"]["message_count"], 1)
        self.assertEqual(actions[0]["content"], "啧。")

    async def test_darkflow_longing_can_become_a_plain_push(self):
        with tempfile.TemporaryDirectory() as root:
            evaluator = LongingBehaviorEvaluator()
            service = BehaviorService(config(root), evaluator)
            darkflow = {
                "cycle_id": 21,
                "stage_index": 1,
                "content": "我想她头发里的味道了。",
                "contexts": [],
                "elapsed_seconds": 3600,
                "sleep_stage": "awake_waiting",
            }
            state = {
                "pipes": {"想靠近": 0.64, "想黏着": 0.48},
                "dominant": "想靠近",
                "dominant_value": 0.64,
            }
            result = await service.process(darkflow, state, None)
            actions = await service.store.list()

        self.assertEqual(result["status"], "rehearsal")
        self.assertEqual(actions[0]["content"], "我想你了。")
        self.assertEqual(evaluator.calls[0]["darkflow"], darkflow["content"])
        self.assertEqual(evaluator.calls[0]["pipes"]["想靠近"], 0.64)

    async def test_presence_only_first_stage_uses_hormones_without_stale_mailbox(self):
        with tempfile.TemporaryDirectory() as root:
            evaluator = LongingBehaviorEvaluator()
            service = BehaviorService(config(root), evaluator)
            result = await service.process(
                {
                    "cycle_id": 31,
                    "stage_index": 1,
                    "content": "半小时没动静，我开始有点惦记。",
                    "contexts": [],
                    "event_count": 0,
                    "mailbox_message_id": None,
                    "elapsed_seconds": 1800,
                    "sleep_stage": "awake_waiting",
                },
                {
                    "pipes": {"想靠近": 0.52},
                    "dominant": "想靠近",
                    "dominant_value": 0.52,
                },
                {"message_id": 8, "message": "这是一封旧信"},
            )

        self.assertEqual(result["status"], "rehearsal")
        self.assertIsNone(evaluator.calls[0]["mailbox_context"])
        self.assertTrue(evaluator.calls[0]["timing"]["presence_only"])
        self.assertFalse(evaluator.calls[0]["timing"]["first_silence_nudge"])
        self.assertEqual(evaluator.calls[0]["timing"]["interaction_phase"], "absence")

    async def test_mailbox_departure_waits_instead_of_using_a_fixed_fallback(self):
        with tempfile.TemporaryDirectory() as root:
            evaluator = SkippingBehaviorEvaluator()
            service = BehaviorService(config(root), evaluator)
            contexts = [
                {"context_card": "用户说她要出去了，刚准备出门，到了会再说。"}
            ]
            event = {
                "status": "applied",
                "event_id": 41,
                "cycle_id": 20,
                "context_card": contexts[0]["context_card"],
            }
            state = {
                "cycle_id": 20,
                "pipes": {
                    "想知道她在干嘛": 0.72,
                    "责任": 0.35,
                    "想靠近": 0.50,
                },
                "dominant": "想知道她在干嘛",
                "dominant_value": 0.72,
                "elapsed_seconds": 3600,
                "sleep_stage": "awake_waiting",
                "event_contexts": contexts,
            }
            scheduled = await service.schedule_event(event, state)
            candidate = scheduled["item"]
            await service.store.update_candidate(
                candidate["candidate_id"],
                "pending",
                candidate["decision_note"],
                (beijing_now() - timedelta(minutes=1)).isoformat(timespec="seconds"),
            )
            results = await service.process_due(state, None, None)
            actions = await service.store.list()

        self.assertEqual(scheduled["status"], "pending")
        self.assertEqual(candidate["follow_up_required"], 1)
        self.assertEqual(candidate["hormone_name"], "想知道她在干嘛")
        self.assertAlmostEqual(candidate["hormone_drive"], 0.72)
        self.assertEqual(results[0]["status"], "waiting")
        self.assertEqual(actions, [])
        self.assertTrue(evaluator.calls[0]["required_follow_up"])
        self.assertEqual(
            evaluator.calls[0]["hormone_context"]["current_name"],
            "想知道她在干嘛",
        )

    async def test_similar_recent_push_is_regenerated_once(self):
        with tempfile.TemporaryDirectory() as root:
            evaluator = RegeneratingBehaviorEvaluator()
            service = BehaviorService(config(root), evaluator)
            await service.store.remember_fingerprint("我想你了。", "靠近她")
            result = await service.process(
                {"cycle_id": 60, "stage_index": 1, "content": "想她。", "contexts": []},
                {"pipes": {"想靠近": 0.8, "想分享": 0.6}, "dominant": "想靠近"},
                None,
            )

        self.assertEqual(result["status"], "rehearsal")
        self.assertEqual(result["item"]["content"], "回来时给我带一声笑。")
        self.assertEqual(len(evaluator.calls), 2)
        self.assertTrue(evaluator.calls[1]["retry_instruction"])

    async def test_sent_plaintext_is_handed_off_once_then_hard_deleted(self):
        with tempfile.TemporaryDirectory() as root:
            service = BehaviorService(config(root), FakeBehaviorEvaluator())
            item = await service.store.record(
                {
                    "cycle_id": 70,
                    "stage_index": 1,
                    "content": "这句话只交接一次。",
                    "status": "sent",
                    "delivered_at": beijing_now().isoformat(timespec="seconds"),
                }
            )
            first = await service.store.list_pending_handoff()
            deleted = await service.store.purge_handoff([item["action_id"]])
            second = await service.store.list_pending_handoff()
            all_actions = await service.store.list()

        self.assertEqual([row["content"] for row in first], ["这句话只交接一次。"])
        self.assertEqual(deleted, 1)
        self.assertEqual(second, [])
        self.assertEqual(all_actions, [])

    async def test_seen_button_marks_push_but_keeps_it_until_ai_handoff(self):
        with tempfile.TemporaryDirectory() as root:
            service = BehaviorService(config(root), FakeBehaviorEvaluator())
            item = await service.store.record(
                {
                    "cycle_id": 71,
                    "stage_index": 1,
                    "content": "我刚刚想你，所以发了这句话。",
                    "status": "sent",
                    "delivered_at": beijing_now().isoformat(timespec="seconds"),
                }
            )
            acknowledged = await service.store.acknowledge_pending()
            pending = await service.store.list_pending_handoff()
            deleted = await service.store.purge_handoff([item["action_id"]])
            remaining = await service.store.list()

        self.assertEqual(acknowledged["status"], "acknowledged")
        self.assertEqual(acknowledged["count"], 1)
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0]["content"], "我刚刚想你，所以发了这句话。")
        self.assertTrue(pending[0]["acknowledged_at"])
        self.assertEqual(deleted, 1)
        self.assertEqual(remaining, [])

    async def test_seen_push_disappears_from_web_until_next_push(self):
        with tempfile.TemporaryDirectory() as root:
            service = BehaviorService(config(root), FakeBehaviorEvaluator())
            first = await service.store.record(
                {
                    "cycle_id": 73,
                    "stage_index": 1,
                    "content": "第一条已经看过的推送。",
                    "status": "sent",
                    "delivered_at": beijing_now().isoformat(timespec="seconds"),
                }
            )

            before = await service.store.pending_handoff_summary()
            await service.store.acknowledge_pending(first["action_id"])
            after = await service.store.pending_handoff_summary()
            ai_handoff = await service.store.list_pending_handoff()

            second = await service.store.record(
                {
                    "cycle_id": 74,
                    "stage_index": 1,
                    "content": "第二条新推送。",
                    "status": "sent",
                    "delivered_at": beijing_now().isoformat(timespec="seconds"),
                }
            )
            next_push = await service.store.pending_handoff_summary()

        self.assertTrue(before["available"])
        self.assertEqual(before["latest"]["action_id"], first["action_id"])
        self.assertFalse(after["available"])
        self.assertEqual(after["count"], 0)
        self.assertEqual(len(ai_handoff), 1)
        self.assertTrue(ai_handoff[0]["acknowledged_at"])
        self.assertTrue(next_push["available"])
        self.assertEqual(next_push["latest"]["action_id"], second["action_id"])

    async def test_handoff_purge_also_removes_related_candidate_material(self):
        with tempfile.TemporaryDirectory() as root:
            service = BehaviorService(config(root), FakeBehaviorEvaluator())
            candidate = await service.store.upsert_candidate(
                {
                    "cycle_id": 72,
                    "source_event_id": 9001,
                    "created_at": beijing_now().isoformat(timespec="seconds"),
                    "due_at": beijing_now().isoformat(timespec="seconds"),
                    "expires_at": (beijing_now() + timedelta(hours=1)).isoformat(
                        timespec="seconds"
                    ),
                    "event_contexts": [{"context_card": "等待她到家"}],
                }
            )
            item = await service.store.record(
                {
                    "cycle_id": 72,
                    "stage_index": 1_000_000 + candidate["candidate_id"],
                    "content": "到家了吗？",
                    "status": "sent",
                    "delivered_at": beijing_now().isoformat(timespec="seconds"),
                }
            )
            await service.store.purge_handoff([item["action_id"]])
            candidates = await service.store.list_candidates()

        self.assertEqual(candidates, [])

    async def test_returned_home_event_is_not_forced_into_follow_up(self):
        with tempfile.TemporaryDirectory() as root:
            evaluator = SkippingBehaviorEvaluator()
            service = BehaviorService(config(root), evaluator)
            event = {
                "status": "applied",
                "event_id": 42,
                "cycle_id": 20,
                "context_card": "用户已经到家了，事情结束了。",
            }
            state = {
                "cycle_id": 20,
                "pipes": {"想知道她在干嘛": 0.80},
                "event_contexts": [{"context_card": event["context_card"]}],
            }
            scheduled = await service.schedule_event(event, state)

        self.assertEqual(scheduled["status"], "skipped")
        self.assertEqual(scheduled["item"]["follow_up_required"], 0)

    async def test_event_candidate_is_scheduled_then_decided_without_darkflow(self):
        with tempfile.TemporaryDirectory() as root:
            evaluator = FakeBehaviorEvaluator()
            service = BehaviorService(config(root), evaluator)
            event = {
                "status": "applied",
                "event_id": 17,
                "cycle_id": 3,
                "context_card": "她出远门，正在路上。",
            }
            state = {
                "cycle_id": 3,
                "pipes": {"想知道她在干嘛": 0.6},
                "dominant": "想知道她在干嘛",
                "dominant_value": 0.6,
                "elapsed_seconds": 3600,
                "sleep_stage": "awake_waiting",
                "event_contexts": [{"context_card": "她出远门，正在路上。"}],
            }
            scheduled = await service.schedule_event(event, state)
            candidate = scheduled["item"]
            await service.store.update_candidate(
                candidate["candidate_id"],
                "pending",
                "测试到点",
                (beijing_now() - timedelta(minutes=1)).isoformat(timespec="seconds"),
            )
            results = await service.process_due(state, None, None)
            actions = await service.store.list()

        self.assertEqual(scheduled["status"], "pending")
        self.assertEqual(results[0]["status"], "rehearsal")
        self.assertEqual(len(actions), 1)
        self.assertEqual(len(evaluator.schedule_calls), 1)
        self.assertEqual(len(evaluator.calls), 1)
        self.assertEqual(evaluator.calls[0]["darkflow"], "")

    async def test_rehearsal_uses_event_context_and_is_idempotent(self):
        with tempfile.TemporaryDirectory() as root:
            evaluator = FakeBehaviorEvaluator()
            service = BehaviorService(config(root), evaluator)
            darkflow = {
                "cycle_id": 9,
                "stage_index": 1,
                "content": "她出门以后我有点惦记。",
                "contexts": [{"context_card": "她上午出远门，正在路上。"}],
                "elapsed_seconds": 7200,
                "sleep_stage": "awake_waiting",
            }
            state = {"pipes": {"想知道她在干嘛": 0.6}, "dominant": "想知道她在干嘛"}
            first = await service.process(darkflow, state, None)
            second = await service.process(darkflow, state, None)
            items = await service.store.list()

        self.assertEqual(first["status"], "rehearsal")
        self.assertEqual(second["status"], "duplicate")
        self.assertEqual(len(items), 1)
        self.assertEqual(len(evaluator.calls), 1)
        self.assertIn("出远门", evaluator.calls[0]["event_contexts"][0]["context_card"])

    async def test_live_without_key_is_held_and_never_calls_network(self):
        with tempfile.TemporaryDirectory() as root, patch.dict(
            os.environ, {"OMBRE_BARK_DEVICE_KEY": ""}, clear=False
        ):
            service = BehaviorService(config(root, mode="live"), FakeBehaviorEvaluator())
            service._send_bark = AsyncMock()
            result = await service.process(
                {"cycle_id": 2, "stage_index": 1, "content": "想联系她", "contexts": []},
                {"pipes": {}, "dominant": "想靠近"},
                None,
            )

        self.assertEqual(result["status"], "held")
        service._send_bark.assert_not_awaited()
        self.assertNotIn("device", str(result["item"].get("context", {})).lower())

    async def test_successful_live_send_flows_back_once(self):
        with tempfile.TemporaryDirectory() as root, patch.dict(
            os.environ, {"OMBRE_BARK_DEVICE_KEY": "test-device"}, clear=False
        ):
            service = BehaviorService(
                config(root, mode="live"), StrongLongingBehaviorEvaluator()
            )
            service._send_messages = AsyncMock()
            feedback = AsyncMock(return_value={"status": "applied"})
            service.set_feedback_callback(feedback)
            darkflow = {
                "cycle_id": 31,
                "stage_index": 2,
                "content": "很想她。",
                "contexts": [],
            }
            state = {"pipes": {"想靠近": 0.86}, "dominant": "想靠近"}
            first = await service.process(darkflow, state, None)
            second = await service.process(darkflow, state, None)

        self.assertEqual(first["status"], "sent")
        self.assertEqual(second["status"], "duplicate")
        feedback.assert_awaited_once_with(
            31,
            "啧。\n突然特别想你。\n想得有点烦。",
            {"想靠近": 0.04, "想分享": -0.02},
        )


if __name__ == "__main__":
    unittest.main()
