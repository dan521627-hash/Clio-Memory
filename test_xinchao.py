import json
import os
import sqlite3
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import server
from utils import BEIJING_TIMEZONE
from xinchao_engine import XinchaoEngine, empty_pipes
from xinchao_store import XinchaoService


class FakeEvaluator:
    prompt_hash = "fake-prompt"

    def __init__(self, tag="同一事件", fail=False, handoff_ready=True):
        self.tag = tag
        self.fail = fail
        self.handoff_ready = handoff_ready
        self.calls = 0

    async def evaluate(self, content):
        self.calls += 1
        if self.fail:
            raise RuntimeError("api unavailable")
        return {
            "event": content[:30],
            "event_tag": self.tag,
            "context_card": f"事件卡：{content[:80]}",
            "severity": 0.6,
            "pipes": {"生气": 0.5, "难过": 0.3, "想靠近": 0.2},
            "narrative_complete": True,
            "handoff_ready": self.handoff_ready,
            "quality_note": "",
        }

    async def monologue(self, pipes, event_summary, obsessions):
        return "测试沉淀"

    async def darkflow(
        self,
        pipes,
        event_contexts,
        obsessions,
        mailbox_context=None,
        **kwargs,
    ):
        return "测试暗涌"


class ProgressiveEvaluator(FakeEvaluator):
    def __init__(self):
        super().__init__(tag="递进事件")
        self.darkflow_inputs = []

    async def darkflow(
        self,
        pipes,
        event_contexts,
        obsessions,
        mailbox_context=None,
        **kwargs,
    ):
        self.darkflow_inputs.append(kwargs)
        previous = kwargs.get("previous_darkflow", "")
        stage = kwargs.get("timing", {}).get("stage_index", 0)
        return f"第{stage}版已经融入上一版：{previous}" + ("感受。" * 220)


class AftereffectEvaluator(FakeEvaluator):
    async def darkflow(self, *args, **kwargs):
        return {
            "text": "这段沉默让我心里又起了一点波动。",
            "aftereffect": {"想靠近": 0.5, "醋": 0.5, "非法状态": 0.8},
        }


class PresenceOnlyEvaluator(FakeEvaluator):
    def __init__(self):
        super().__init__()
        self.input = None

    async def darkflow(
        self,
        pipes,
        event_contexts,
        obsessions,
        mailbox_context=None,
        **kwargs,
    ):
        self.input = {
            "event_contexts": event_contexts,
            "mailbox_context": mailbox_context,
            **kwargs,
        }
        return "她已经半小时没说话了。我有点惦记，但不知道她具体在忙什么。"


class PrivateThoughtEvaluator(FakeEvaluator):
    async def evaluate(self, content):
        self.calls += 1
        return {
            "event": content[:30],
            "event_tag": f"事件-{self.calls}",
            "context_card": f"事件卡：{content[:80]}",
            "severity": 0.5,
            "pipes": {"醋": 0.12, "想靠近": 0.08},
            "inner_thoughts": [
                {
                    "text": "我有点不体面地想把她留下来。",
                    "tag": "想把她留下来",
                    "tone": "negative",
                    "intensity": 0.72,
                    "reason": "这件事碰到了占有欲。",
                }
            ],
            "narrative_complete": True,
            "handoff_ready": True,
            "quality_note": "",
        }


class PrivateThoughtDarkflowEvaluator(PrivateThoughtEvaluator):
    def __init__(self):
        super().__init__()
        self.darkflow_thoughts = []

    async def darkflow(
        self,
        pipes,
        event_contexts,
        obsessions,
        mailbox_context=None,
        **kwargs,
    ):
        self.darkflow_thoughts = list(obsessions)
        return {"text": "这条没说出口的心念仍在影响我。", "aftereffect": {}}


def config(root, **overrides):
    settings = {
        "enabled": True,
        "db_path": os.path.join(root, "xinchao.sqlite3"),
        "monologue_enabled": False,
        "paraphrase_dedupe_seconds": 600,
        "exact_dedupe_hours": 24,
    }
    settings.update(overrides)
    return {
        "buckets_dir": root,
        "dehydration": {"api_key": ""},
        "xinchao": settings,
    }


class XinchaoEngineTests(unittest.TestCase):
    def test_evaluator_json_parser_accepts_fenced_or_prefixed_json(self):
        from xinchao_evaluator import XinchaoEvaluator

        fenced = '说明如下：\n```json\n{"event":"测试","pipes":{}}\n```'
        self.assertEqual(XinchaoEvaluator._clean_json(fenced)["event"], "测试")

    def test_private_judge_config_is_filtered_and_changes_prompt_hash(self):
        from xinchao_evaluator import XinchaoEvaluator

        with tempfile.TemporaryDirectory() as root:
            judge_path = Path(root) / "judge.json"
            judge_path.write_text(
                json.dumps(
                    {
                        "custom_rules": "只按原文判断",
                        "relations": [
                            {
                                "name": "测试人物",
                                "aliases": ["别名"],
                                "role": "朋友",
                                "safety": "安全",
                                "trigger": {"开心": 0.2, "不存在的管子": 9},
                            }
                        ],
                        "unknown_field": "不得进入裁判上下文",
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            evaluator = XinchaoEvaluator(
                {
                    "dehydration": {"api_key": ""},
                    "xinchao": {"judge_config_path": str(judge_path)},
                }
            )
            context = evaluator._judge_context()
            first_hash = evaluator.prompt_hash
            self.assertIn("测试人物", context)
            self.assertIn("只按原文判断", context)
            self.assertNotIn("不存在的管子", context)
            self.assertNotIn("unknown_field", context)

            judge_path.write_text('{"custom_rules":"改后的规则"}', encoding="utf-8")
            self.assertNotEqual(first_hash, evaluator.prompt_hash)

    def test_judge_config_write_preserves_previous_and_filters_fields(self):
        from xinchao_evaluator import XinchaoEvaluator

        with tempfile.TemporaryDirectory() as root:
            judge_path = Path(root) / "hormone-judge.json"
            judge_path.write_text(
                json.dumps({"custom_rules": "上一版", "relations": []}, ensure_ascii=False),
                encoding="utf-8",
            )
            evaluator = XinchaoEvaluator(
                {
                    "dehydration": {"api_key": ""},
                    "xinchao": {"judge_config_path": str(judge_path)},
                }
            )
            result = evaluator.write_judge_config(
                {
                    "custom_rules": "新规则",
                    "relations": [
                        {
                            "name": "测试人物",
                            "aliases": ["别名"],
                            "role": "朋友",
                            "safety": "安全",
                            "trigger": {"开心": 0.2, "非法状态": 1},
                            "note": "测试关系",
                            "unknown": "不能保存",
                        }
                    ],
                    "unknown_root": "不能保存",
                }
            )
            previous = Path(root) / "hormone-judge.previous.json"
            self.assertTrue(previous.is_file())
            self.assertIn("上一版", previous.read_text(encoding="utf-8"))
            self.assertEqual(result["custom_rules"], "新规则")
            stored = evaluator.read_judge_config()
            self.assertEqual(stored["relations"][0]["trigger"], {"开心": 0.2})
            self.assertNotIn("unknown_root", judge_path.read_text(encoding="utf-8"))
            self.assertNotIn("unknown", judge_path.read_text(encoding="utf-8"))

    def test_negative_emotions_ebb_gradually_with_distinct_speeds(self):
        engine = XinchaoEngine({"xinchao": {"step_minutes": 10}})
        start = datetime(2026, 8, 1, 8, 0, tzinfo=BEIJING_TIMEZONE)
        pipes = empty_pipes()
        pipes.update({"生气": 0.8, "醋": 0.8, "难过": 0.8, "自省": 0.8})
        result = engine.evolve(pipes, start, start + timedelta(hours=2))
        self.assertLess(result["生气"], result["醋"])
        self.assertLess(result["醋"], result["难过"])
        self.assertLess(result["难过"], result["自省"])
        self.assertGreater(result["生气"], 0.0)

    def test_negative_values_are_bounded(self):
        engine = XinchaoEngine({"xinchao": {"negative_cap": 0.85}})
        result = engine.apply_event(empty_pipes(), {"生气": 0.8, "难过": 0.8})
        result = engine.apply_event(result, {"生气": 0.8, "难过": 0.8})
        self.assertEqual(result["生气"], 0.85)
        self.assertEqual(result["难过"], 0.85)

    def test_personality_baselines_survive_negative_events(self):
        engine = XinchaoEngine({
            "xinchao": {"baseline": {"性欲": 0.15, "想靠近": 0.18}}
        })
        floors = engine.baseline_pipes()
        result = engine.apply_event(
            empty_pipes(), {"性欲": -0.8, "想靠近": -0.8}, floors
        )
        self.assertEqual(result["性欲"], 0.15)
        self.assertEqual(result["想靠近"], 0.18)
        self.assertEqual(result["生气"], 0.0)

    def test_sleep_stops_growth_but_negative_emotions_keep_ebbing(self):
        engine = XinchaoEngine({"xinchao": {"step_minutes": 10}})
        start = datetime(2026, 8, 1, 0, 0, tzinfo=BEIJING_TIMEZONE)
        pipes = empty_pipes()
        pipes.update({"想靠近": 0.2, "生气": 0.8})
        result = engine.evolve_absence(
            pipes,
            start,
            start + timedelta(hours=20),
            drowsy_after_hours=4,
            sleep_after_hours=7,
        )
        awake_only = engine.evolve(
            pipes, start, start + timedelta(hours=20)
        )
        self.assertLess(result["想靠近"], awake_only["想靠近"])
        self.assertLess(result["生气"], pipes["生气"])

    def test_satisfaction_plateau_pauses_only_the_named_drive(self):
        engine = XinchaoEngine({"xinchao": {"step_minutes": 10}})
        start = datetime(2026, 8, 1, 8, 0, tzinfo=BEIJING_TIMEZONE)
        pipes = empty_pipes()
        pipes.update({"想靠近": 0.2, "想分享": 0.2})
        result = engine.evolve(
            pipes,
            start,
            start + timedelta(hours=2),
            plateaus={"想靠近": start + timedelta(hours=3)},
        )
        self.assertEqual(result["想靠近"], pipes["想靠近"])
        self.assertGreater(result["想分享"], pipes["想分享"])

    def test_concurrent_schema_initialization_is_idempotent(self):
        with tempfile.TemporaryDirectory() as root:
            with ThreadPoolExecutor(max_workers=4) as executor:
                services = list(
                    executor.map(lambda _: XinchaoService(config(root)), range(4))
                )
            self.assertEqual(len(services), 4)
            with sqlite3.connect(services[0].db_path) as connection:
                columns = [
                    row[1]
                    for row in connection.execute(
                        "PRAGMA table_info(xinchao_state)"
                    ).fetchall()
                ]
            self.assertEqual(columns.count("sleep_started_at"), 1)


class XinchaoServiceTests(unittest.IsolatedAsyncioTestCase):
    async def test_ordinary_write_updates_hormones_without_starting_static(self):
        with tempfile.TemporaryDirectory() as root:
            service = XinchaoService(
                config(root, monologue_enabled=True, darkflow_stage_hours=[0])
            )
            service.evaluator = FakeEvaluator(
                tag="普通写入", handoff_ready=False
            )
            recorded = await service.record_event(
                "我记下刚发生的事，也写了当时的心情。", "mailbox", "ordinary"
            )
            state = await service.status()
            settled = await service.settle_darkflow()

        self.assertEqual(recorded["status"], "applied")
        self.assertFalse(recorded["handoff_ready"])
        self.assertEqual(state["interaction_phase"], "active")
        self.assertFalse(state["static_ready"])
        self.assertEqual(state["elapsed_seconds"], 0)
        self.assertEqual(settled["status"], "waiting")
        self.assertEqual(settled["phase"], "active")

    async def test_explicit_handoff_arms_static_countdown(self):
        with tempfile.TemporaryDirectory() as root:
            service = XinchaoService(config(root, monologue_enabled=True))
            service.evaluator = FakeEvaluator(tag="窗口收尾", handoff_ready=True)
            recorded = await service.record_event(
                "这一轮聊完了，我把事情和感受归档，准备换窗口。",
                "mailbox",
                "handoff",
            )
            state = await service.status()

        self.assertTrue(recorded["handoff_ready"])
        self.assertTrue(state["static_ready"])
        self.assertEqual(state["interaction_phase"], "absence")
        self.assertIsNotNone(state["static_started_at"])

    async def test_heartbeat_does_not_open_cycle_or_generate_darkflow(self):
        with tempfile.TemporaryDirectory() as root:
            service = XinchaoService(
                config(root, monologue_enabled=True, darkflow_stage_hours=[0])
            )
            evaluator = PresenceOnlyEvaluator()
            service.evaluator = evaluator

            observed = await service.observe_presence(
                session_id="session-a",
                source="mcp:heartbeat",
                event_id="turn-a",
                start_cycle=True,
            )
            status = await service.status()
            waiting = await service.settle_darkflow(
                mailbox_context={
                    "message_id": 99,
                    "created_at": "2026-08-08T08:00:00+08:00",
                    "message": "旧信箱不应冒充本轮新事件",
                }
            )
            silence_darkflow = await service.pending_darkflow()
            settled = await service.settle_darkflow(
                mailbox_context={
                    "message_id": 99,
                    "created_at": "2026-08-08T08:00:00+08:00",
                    "message": "旧信箱不应冒充本轮新事件",
                }
            )
            darkflow = await service.pending_darkflow()
            with sqlite3.connect(service.db_path) as connection:
                event_count = connection.execute(
                    "SELECT COUNT(*) FROM xinchao_events"
                ).fetchone()[0]

            self.assertFalse(observed["cycle_started"])
            self.assertTrue(status["repeated"])
            self.assertEqual(event_count, 0)
            self.assertEqual(waiting["status"], "idle")
            self.assertIsNone(silence_darkflow)
            self.assertEqual(settled["status"], "idle")
            self.assertIsNone(darkflow)
            self.assertIsNone(evaluator.input)

            refreshed = await service.observe_presence(
                session_id="session-a",
                source="mcp:heartbeat",
                event_id="turn-b",
                start_cycle=True,
            )
            self.assertFalse(refreshed["cycle_started"])
            self.assertIsNone(await service.pending_darkflow())

    async def test_private_flash_becomes_obsession_without_entering_boot_text(self):
        with tempfile.TemporaryDirectory() as root:
            service = XinchaoService(config(root, obsession_repeats=2))
            service.evaluator = PrivateThoughtEvaluator()
            await service.record_event("第一次发生的完整事件和感受。", "hold", "a")
            first = await service.list_private_thoughts("flash")
            await service.record_event("第二次发生的另一件完整事件和感受。", "hold", "b")
            obsessions = await service.list_private_thoughts("obsession")
            compact = service.render_compact(await service.status())

            self.assertEqual(len(first), 1)
            self.assertEqual(len(obsessions), 1)
            self.assertEqual(obsessions[0]["privacy"], "inner_only")
            self.assertNotIn("想把她留下来", compact)
            self.assertTrue(
                await service.resolve_private_thought(obsessions[0]["canonical_tag"])
            )
            self.assertEqual(len(await service.list_private_thoughts("resolved")), 1)

    async def test_default_darkflow_stages_end_at_twelve_hours(self):
        with tempfile.TemporaryDirectory() as root:
            service = XinchaoService(
                config(root, monologue_enabled=True, monologue_after_hours=1)
            )
        self.assertEqual(service.darkflow_stage_hours, [1, 2, 4, 6, 8, 10, 12])
        self.assertEqual(service.deep_sleep_after_hours, 12)
        self.assertEqual(service._sleep_stage(4 * 3600), "drowsy")
        self.assertEqual(service._sleep_stage(6 * 3600), "light_sleep")
        self.assertEqual(service._sleep_stage(8 * 3600), "dreaming")
        self.assertEqual(service._sleep_stage(10 * 3600), "deep_sleep")
        self.assertEqual(service._sleep_stage(12 * 3600), "hibernating")

    async def test_presence_nudge_is_not_a_darkflow_stage(self):
        with tempfile.TemporaryDirectory() as root:
            service = XinchaoService(config(root, monologue_after_hours=1))

        self.assertEqual(service._target_stage(29 * 60, "presence"), 0)
        self.assertEqual(service._target_stage(30 * 60, "presence"), 0)
        self.assertEqual(service._target_stage(60 * 60, "presence"), 1)
        self.assertEqual(service._target_stage(30 * 60, "event"), 0)

    async def test_presence_alone_never_starts_legacy_half_hour_darkflow(self):
        with tempfile.TemporaryDirectory() as root:
            service = XinchaoService(
                config(root, monologue_enabled=True, darkflow_stage_hours=[0])
            )
            service.evaluator = PresenceOnlyEvaluator()
            await service.observe_presence(
                session_id="session-old",
                source="mcp:pulse_boot",
                event_id="turn-old",
                start_cycle=True,
            )
            old_stamp = (
                datetime.now(BEIJING_TIMEZONE) - timedelta(minutes=61)
            ).isoformat(timespec="seconds")
            with sqlite3.connect(service.db_path) as connection:
                connection.execute(
                    "UPDATE xinchao_state SET last_event_at=?, last_presence_at=? "
                    "WHERE state_id=1",
                    (old_stamp, old_stamp),
                )
            self.assertEqual((await service.settle_darkflow())["status"], "idle")

            boot = await service.consume_boot()

        self.assertIsNone(boot.get("darkflow_item"))
        self.assertEqual(boot.get("darkflow"), "")

    async def test_darkflow_aftereffect_is_bounded_and_applied_once(self):
        with tempfile.TemporaryDirectory() as root:
            service = XinchaoService(
                config(root, monologue_enabled=True, darkflow_stage_hours=[0])
            )
            service.evaluator = AftereffectEvaluator()
            await service.record_event("我写清了一件事和当时的感受。", "hold", "a")
            before = await service.status()
            first = await service.settle_darkflow()
            after_first = await service.status()
            second = await service.settle_darkflow()
            after_second = await service.status()
            with sqlite3.connect(service.db_path) as connection:
                row = connection.execute(
                    "SELECT aftereffect_json, aftereffect_applied_at "
                    "FROM xinchao_darkflow WHERE slot_id=1"
                ).fetchone()

        self.assertEqual(first["status"], "updated")
        self.assertEqual(second["status"], "waiting")
        self.assertLessEqual(after_first["pipes"]["想靠近"] - before["pipes"]["想靠近"], 0.0801)
        self.assertAlmostEqual(
            after_first["pipes"]["想靠近"],
            after_second["pipes"]["想靠近"],
            places=4,
        )
        self.assertAlmostEqual(
            after_first["pipes"]["醋"], after_second["pipes"]["醋"], places=4
        )
        saved = json.loads(row[0])
        self.assertEqual(saved, {"想靠近": 0.08, "醋": 0.08})
        self.assertIsNotNone(row[1])

    async def test_memory_resonance_reaches_darkflow_without_changing_buckets(self):
        with tempfile.TemporaryDirectory() as root:
            service = XinchaoService(
                config(root, monologue_enabled=True, darkflow_stage_hours=[0])
            )
            evaluator = ProgressiveEvaluator()
            service.evaluator = evaluator
            resonance = [
                {
                    "bucket_id": "old-1",
                    "name": "以前等她回来",
                    "excerpt": "那次我也很想她。",
                    "similarity": 0.82,
                }
            ]
            provider = AsyncMock(return_value=resonance)
            service.set_memory_resonance_provider(provider)
            await service.record_event("我写下了一件事和当时的感受。", "hold", "a")
            result = await service.settle_darkflow()

        self.assertEqual(result["status"], "updated")
        provider.assert_awaited_once()
        self.assertEqual(
            evaluator.darkflow_inputs[0]["memory_resonance"], resonance
        )

    async def test_private_thought_text_reason_and_intensity_reach_darkflow(self):
        with tempfile.TemporaryDirectory() as root:
            service = XinchaoService(
                config(root, monologue_enabled=True, darkflow_stage_hours=[0])
            )
            evaluator = PrivateThoughtDarkflowEvaluator()
            service.evaluator = evaluator
            await service.record_event("她要离开一会儿，我心里有点舍不得。", "hold", "a")
            result = await service.settle_darkflow()

        self.assertEqual(result["status"], "updated")
        self.assertEqual(len(evaluator.darkflow_thoughts), 1)
        thought = evaluator.darkflow_thoughts[0]
        self.assertEqual(thought["thought_text"], "我有点不体面地想把她留下来。")
        self.assertEqual(thought["tone"], "negative")
        self.assertAlmostEqual(thought["intensity"], 0.72)
        self.assertEqual(thought["reason"], "这件事碰到了占有欲。")

    def test_manager_resonance_card_shows_source_title_and_excerpt(self):
        script = (Path(__file__).with_name("manager") / "manager.js").read_text(
            encoding="utf-8"
        )
        self.assertIn("item.excerpt", script)
        self.assertIn("信箱留言 #", script)
        self.assertIn("旧信箱", script)

    async def test_behavior_feedback_can_deepen_and_release_but_is_bounded(self):
        with tempfile.TemporaryDirectory() as root:
            service = XinchaoService(config(root))
            service.evaluator = FakeEvaluator()
            event = await service.record_event(
                "我写下完整事件和自己的感受。", "hold", "a"
            )
            before = await service.status()
            deferred = await service.apply_behavior_feedback(
                event["cycle_id"],
                "我想你了。",
                {"想靠近": 0.8, "想分享": -0.8, "非法状态": 0.5},
            )
            applied = await service.apply_behavior_feedback(
                event["cycle_id"],
                "我想你了。",
                {"想靠近": 0.8, "想分享": -0.8, "非法状态": 0.5},
            )
            after = await service.status()
            transitions = await service.recent_transitions()

        self.assertEqual(deferred["status"], "deferred")
        self.assertEqual(deferred["deltas"], {})
        self.assertEqual(applied["deltas"], {"想靠近": 0.02, "想分享": -0.02})
        self.assertLessEqual(after["pipes"]["想靠近"] - before["pipes"]["想靠近"], 0.0201)
        self.assertLessEqual(before["pipes"]["想分享"] - after["pipes"]["想分享"], 0.0201)
        self.assertEqual(transitions[0]["transition_type"], "behavior_feedback_applied")
        self.assertNotIn("我想你了", json.dumps(transitions, ensure_ascii=False))

    async def test_evaluator_disables_thinking_and_retries_empty_json(self):
        from xinchao_evaluator import XinchaoEvaluator

        evaluator = XinchaoEvaluator(
            {
                "dehydration": {"api_key": "test-key"},
                "xinchao": {"model": "test-model"},
            }
        )
        empty = SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=""))]
        )
        valid = SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(
                        content=json.dumps(
                            {
                                "event": "测试事件",
                                "event_tag": "测试",
                                "severity": 0.2,
                                "pipes": {"开心": 0.2},
                                "narrative_complete": True,
                                "quality_note": "",
                            },
                            ensure_ascii=False,
                        )
                    )
                )
            ]
        )
        create = AsyncMock(side_effect=[empty, valid])
        evaluator.client = SimpleNamespace(
            chat=SimpleNamespace(completions=SimpleNamespace(create=create))
        )
        result = await evaluator.evaluate("我完成了测试，心里有点开心。")
        self.assertEqual(result["event_tag"], "测试")
        self.assertEqual(create.await_count, 2)
        self.assertEqual(
            create.await_args.kwargs["extra_body"],
            {"thinking": {"type": "disabled"}},
        )

    async def test_exact_and_short_paraphrase_duplicates_do_not_reapply(self):
        with tempfile.TemporaryDirectory() as root:
            service = XinchaoService(config(root))
            service.evaluator = FakeEvaluator()
            first = await service.record_event("我很生气，但我知道自己在想什么。", "hold", "a")
            exact = await service.record_event("我很生气，但我知道自己在想什么。", "mailbox", "1")
            paraphrase = await service.record_event("这事让我气得够呛，我心里一直堵着。", "hold", "b")
            self.assertEqual(first["status"], "applied")
            self.assertEqual(exact["status"], "duplicate")
            self.assertEqual(paraphrase["status"], "duplicate")
            with sqlite3.connect(service.db_path) as connection:
                applied = connection.execute(
                    "SELECT COUNT(*) FROM xinchao_events WHERE status='applied'"
                ).fetchone()[0]
            self.assertEqual(applied, 1)

    async def test_explicit_event_id_is_idempotent_even_when_text_changes(self):
        with tempfile.TemporaryDirectory() as root:
            service = XinchaoService(config(root))
            service.evaluator = FakeEvaluator(tag="第一件事")
            first = await service.record_event(
                "第一次提交的完整事件和感受。",
                "hold",
                "a",
                external_event_id="window-7-event-9",
            )
            second = await service.record_event(
                "网络重试时文字略有变化。",
                "hold",
                "a",
                external_event_id="window-7-event-9",
            )
            self.assertEqual(first["status"], "applied")
            self.assertEqual(second["status"], "duplicate")
            self.assertEqual(second["reason"], "event_id")

    async def test_progressive_darkflow_rewrites_one_slot_and_hibernates_at_48h(self):
        with tempfile.TemporaryDirectory() as root:
            service = XinchaoService(
                config(
                    root,
                    monologue_enabled=True,
                    darkflow_stage_hours=[2, 4, 48],
                    darkflow_max_chars=400,
                )
            )
            evaluator = ProgressiveEvaluator()
            service.evaluator = evaluator
            await service.record_event(
                "我记下了完整事件和当时的感受。", "hold", "a"
            )

            async def set_absence(hours):
                moment = datetime.now(BEIJING_TIMEZONE) - timedelta(hours=hours)
                with sqlite3.connect(service.db_path) as connection:
                    connection.execute(
                        "UPDATE xinchao_state SET last_event_at=?, last_presence_at=?, "
                        "static_started_at=? WHERE state_id=1",
                        (
                            moment.isoformat(timespec="seconds"),
                            moment.isoformat(timespec="seconds"),
                            moment.isoformat(timespec="seconds"),
                        ),
                    )

            await set_absence(2.2)
            self.assertEqual((await service.settle_darkflow())["stage_index"], 1)
            first = await service.darkflow_status()
            self.assertEqual(first["stage_index"], 1)
            self.assertLessEqual(len(first["content"]), 400)

            await set_absence(4.2)
            self.assertEqual((await service.settle_darkflow())["stage_index"], 2)
            second = await service.darkflow_status()
            self.assertEqual(second["stage_index"], 2)
            self.assertGreater(second["revision"], first["revision"])
            self.assertIn(first["content"][:40], evaluator.darkflow_inputs[-1]["previous_darkflow"])

            await set_absence(48.2)
            self.assertEqual((await service.settle_darkflow())["stage_index"], 3)
            final = await service.darkflow_status()
            self.assertEqual(final["sleep_stage"], "hibernating")
            self.assertIsNone(final["next_stage_at"])
            with sqlite3.connect(service.db_path) as connection:
                count = connection.execute(
                    "SELECT COUNT(*) FROM xinchao_darkflow"
                ).fetchone()[0]
            self.assertEqual(count, 1)

    async def test_boot_delivery_is_once_per_hashed_session(self):
        with tempfile.TemporaryDirectory() as root:
            service = XinchaoService(config(root))
            self.assertIsNone(await service.boot_delivery("session-a"))
            await service.record_boot_delivery("session-a", "开机正文")
            self.assertIsNotNone(await service.boot_delivery("session-a"))
            self.assertIsNone(await service.boot_delivery("session-b"))
            with sqlite3.connect(service.db_path) as connection:
                stored = connection.execute(
                    "SELECT session_hash FROM xinchao_boot_deliveries"
                ).fetchone()[0]
            self.assertNotIn("session-a", stored)

    async def test_transition_journal_never_contains_narrative_body(self):
        with tempfile.TemporaryDirectory() as root:
            service = XinchaoService(config(root))
            service.evaluator = FakeEvaluator()
            secret_body = "这段正文绝不能进入状态流水"
            await service.record_event(secret_body, "hold", "a")
            transitions = await service.recent_transitions()
            self.assertTrue(transitions)
            self.assertNotIn(secret_body, json.dumps(transitions, ensure_ascii=False))

    async def test_manager_status_endpoint_is_read_only(self):
        import manager_server

        expected = {
            "available": True,
            "cycle_id": 7,
            "pipes": {"开心": 0.2},
            "dominant": "开心",
            "dominant_value": 0.2,
        }
        with patch.object(
            manager_server.xinchao_service,
            "status",
            new=AsyncMock(return_value=expected),
        ) as status:
            result = await manager_server.xinchao_status()
        status.assert_awaited_once_with()
        self.assertEqual(result["display_name"], "激素")
        self.assertEqual(result["cycle_id"], 7)

    async def test_manager_judge_editor_saves_and_hot_reloads(self):
        import manager_server
        from xinchao_evaluator import XinchaoEvaluator

        with tempfile.TemporaryDirectory() as root:
            judge_path = Path(root) / "judge.json"
            judge_path.write_text('{"custom_rules":"旧规则","relations":[]}', encoding="utf-8")
            evaluator = XinchaoEvaluator(
                {
                    "dehydration": {"api_key": "secret-not-for-ui"},
                    "xinchao": {"judge_config_path": str(judge_path)},
                }
            )
            old_hash = evaluator.prompt_hash
            payload = manager_server.JudgeConfigUpdate(
                custom_rules="新规则",
                proxy_voice="短、直、第一人称",
                baselines={"性欲": 0.22, "想靠近": 0.20},
                relations=[
                    manager_server.JudgeRelation(
                        name="测试人物",
                        aliases=["别名"],
                        role="朋友",
                        safety="安全",
                        trigger={"开心": 0.25},
                        note="出现时结合正文判断",
                    )
                ],
            )
            with patch.object(manager_server.xinchao_service, "evaluator", evaluator):
                saved = await manager_server.update_xinchao_judge(payload)
                loaded = await manager_server.xinchao_judge()
            self.assertTrue(saved["hot_reload"])
            self.assertNotEqual(old_hash, saved["prompt_hash"])
            self.assertEqual(loaded["relations"][0]["name"], "测试人物")
            self.assertEqual(loaded["baselines"]["性欲"], 0.22)
            self.assertEqual(loaded["proxy_voice"], "短、直、第一人称")
            self.assertNotIn("secret-not-for-ui", json.dumps(loaded, ensure_ascii=False))

    async def test_manager_darkflow_read_does_not_mark_delivered(self):
        import manager_server

        expected = {
            "cycle_id": 3,
            "status": "pending",
            "content": "测试暗涌",
        }
        with patch.object(
            manager_server.xinchao_service,
            "darkflow_status",
            new=AsyncMock(return_value=expected),
        ) as status:
            result = await manager_server.xinchao_darkflow()
        status.assert_awaited_once_with()
        self.assertTrue(result["available"])
        self.assertEqual(result["item"], expected)

    async def test_boot_is_repeatable_and_does_not_reset_state(self):
        with tempfile.TemporaryDirectory() as root:
            service = XinchaoService(config(root))
            service.evaluator = FakeEvaluator(tag="第一件事")
            await service.record_event("我记下第一件事，也写清了当时的感受。", "hold", "a")
            first = await service.consume_boot()
            repeated = await service.consume_boot()
            self.assertFalse(first["repeated"])
            self.assertFalse(repeated["repeated"])
            self.assertEqual(first["cycle_id"], repeated["cycle_id"])
            with sqlite3.connect(service.db_path) as connection:
                stored_pipes = json.loads(
                    connection.execute(
                        "SELECT pipes_json FROM xinchao_state WHERE state_id=1"
                    ).fetchone()[0]
                )
            self.assertEqual(stored_pipes["生气"], 0.5)
            for name, value in stored_pipes.items():
                self.assertAlmostEqual(first["pipes"][name], value, places=3)
                self.assertAlmostEqual(repeated["pipes"][name], value, places=3)

            service.evaluator = FakeEvaluator(tag="第二件事")
            await service.record_event("后来发生了另一件事，我也有新的感受。", "hold", "b")
            second = await service.consume_boot()
            self.assertGreater(second["cycle_id"], first["cycle_id"])

    async def test_seen_acknowledgement_partly_settles_without_timer(self):
        with tempfile.TemporaryDirectory() as root:
            service = XinchaoService(
                config(root, monologue_enabled=True, monologue_after_hours=0)
            )
            service.evaluator = FakeEvaluator(tag="等待回应")
            await service.record_event(
                "我刚刚发出一条想念，也写清了当时感受。", "mailbox", "seen"
            )
            await service.settle_darkflow()
            moment = datetime(2026, 8, 11, 20, 0, tzinfo=BEIJING_TIMEZONE)
            pipes = service._baseline_floors()
            pipes.update(
                {
                    "想靠近": 0.80,
                    "想黏着": 0.70,
                    "想知道她在干嘛": 0.65,
                    "想分享": 0.50,
                    "性欲": 0.60,
                    "生气": 0.55,
                }
            )
            with sqlite3.connect(service.db_path) as connection:
                connection.execute(
                    "UPDATE xinchao_state SET pipes_json=?, last_event_at=?, "
                    "last_presence_at=? WHERE state_id=1",
                    (
                        json.dumps(pipes, ensure_ascii=False),
                        moment.isoformat(),
                        moment.isoformat(),
                    ),
                )
            with patch("xinchao_store.beijing_now", return_value=moment):
                before = await service.status()
                darkflow_before = await service.pending_darkflow()
                self.assertIsNotNone(darkflow_before)
                result = await service.acknowledge_seen()
                after = await service.status()
                darkflow_after = await service.pending_darkflow()

            self.assertEqual(result["status"], "acknowledged")
            self.assertEqual(result["previous_cycle_id"], before["cycle_id"])
            self.assertEqual(after["cycle_id"], before["cycle_id"] + 1)
            self.assertTrue(after["available"])
            self.assertEqual(after["cycle_origin"], "acknowledgement")
            self.assertEqual(after["interaction_phase"], "active")
            self.assertFalse(after["static_ready"])
            self.assertIsNone(result["silence_started_at"])
            self.assertTrue(result["pending_darkflow_carried"])
            self.assertIsNotNone(darkflow_after)
            self.assertEqual(darkflow_after["cycle_id"], after["cycle_id"])
            self.assertEqual(darkflow_after["content"], darkflow_before["content"])
            self.assertEqual(darkflow_after["status"], "pending")
            self.assertGreater(after["pipes"]["想靠近"], 0.18)
            self.assertLess(after["pipes"]["想靠近"], before["pipes"]["想靠近"])
            self.assertGreater(after["pipes"]["想黏着"], 0.12)
            self.assertLess(after["pipes"]["想黏着"], before["pipes"]["想黏着"])
            self.assertEqual(after["pipes"]["性欲"], 0.60)
            self.assertEqual(after["pipes"]["生气"], 0.55)
            self.assertEqual(after["pipes"]["开心"], 0.04)
            self.assertEqual(after["pipes"]["满足"], 0.06)
            with sqlite3.connect(service.db_path) as connection:
                transition = connection.execute(
                    "SELECT transition_type FROM xinchao_transitions "
                    "ORDER BY transition_id DESC LIMIT 1"
                ).fetchone()
            self.assertEqual(transition[0], "behavior_acknowledged")

    async def test_darkflow_is_one_slot_and_manager_reads_do_not_consume(self):
        with tempfile.TemporaryDirectory() as root:
            service = XinchaoService(
                config(root, monologue_enabled=True, monologue_after_hours=0)
            )
            service.evaluator = FakeEvaluator(tag="第一轮")
            await service.record_event("第一轮发生了一件事，我也写清了感受。", "hold", "a")
            first = await service.consume_boot(
                mailbox_context={
                    "message_id": 8,
                    "created_at": "2026-08-01T08:00:00+08:00",
                    "message": "上一窗口的主交接",
                }
            )
            self.assertEqual(first["darkflow"], "测试暗涌")
            pending = await service.pending_darkflow()
            self.assertEqual(pending["content"], "测试暗涌")
            self.assertEqual(pending["mailbox_message_id"], 8)
            self.assertEqual(pending["event_count"], 1)

            viewed = await service.darkflow_status()
            self.assertEqual(viewed["status"], "pending")
            viewed_again = await service.darkflow_status()
            self.assertEqual(viewed_again["status"], "pending")

            self.assertTrue(await service.mark_darkflow_delivered(first["cycle_id"]))
            self.assertIsNone(await service.pending_darkflow())
            delivered = await service.darkflow_status()
            self.assertEqual(delivered["status"], "delivered")
            self.assertEqual(delivered["content"], "测试暗涌")

            service.evaluator = FakeEvaluator(tag="第二轮")
            await service.record_event("第二轮又发生了一件新事，我也写清了感受。", "hold", "b")
            second = await service.consume_boot()
            latest = await service.darkflow_status()
            self.assertEqual(latest["cycle_id"], second["cycle_id"])
            with sqlite3.connect(service.db_path) as connection:
                count = connection.execute(
                    "SELECT COUNT(*) FROM xinchao_darkflow"
                ).fetchone()[0]
            self.assertEqual(count, 1)

    async def test_new_write_discards_pending_darkflow_and_restarts_cycle(self):
        with tempfile.TemporaryDirectory() as root:
            service = XinchaoService(
                config(root, monologue_enabled=True, monologue_after_hours=0)
            )
            service.evaluator = FakeEvaluator(tag="第一轮")
            await service.record_event(
                "第一轮发生了一件事，我也写清了感受。", "hold", "a"
            )
            first = await service.consume_boot()
            self.assertEqual(first["darkflow"], "测试暗涌")
            self.assertIsNotNone(await service.pending_darkflow())

            before = await service.status()
            service.evaluator = FakeEvaluator(tag="第二轮", handoff_ready=False)
            result = await service.record_event(
                "后来又写进一件新事，这才是现在最新的交接。", "mailbox", "b"
            )
            after = await service.status()

            self.assertEqual(result["status"], "applied")
            self.assertIsNone(await service.pending_darkflow())
            self.assertEqual(after["cycle_origin"], "event")
            self.assertEqual(after["interaction_phase"], "active")
            self.assertFalse(after["static_ready"])
            self.assertEqual(after["darkflow_stage"], 0)
            self.assertGreaterEqual(after["cycle_id"], before["cycle_id"] + 1)

    async def test_mailbox_can_seed_time_based_darkflow_without_duplicate_event(self):
        with tempfile.TemporaryDirectory() as root:
            service = XinchaoService(
                config(root, monologue_enabled=True, monologue_after_hours=0)
            )
            service.evaluator = FakeEvaluator()
            await service.record_event(
                "我只写了这一封窗口接力信，也写清了当时感受。",
                "mailbox",
                "21",
            )
            result = await service.consume_boot(
                mailbox_context={
                    "message_id": 21,
                    "created_at": "2999-08-01T08:00:00+08:00",
                    "message": "这封信本身已经是主交接",
                }
            )
            self.assertEqual(result["darkflow"], "测试暗涌")
            darkflow = await service.darkflow_status()
            self.assertEqual(darkflow["mailbox_message_id"], 21)
            self.assertEqual(darkflow["event_count"], 0)

    async def test_applied_event_keeps_cycle_context_without_original_body(self):
        with tempfile.TemporaryDirectory() as root:
            service = XinchaoService(config(root))
            service.evaluator = FakeEvaluator()
            result = await service.record_event(
                "有人在晚上来找我，我当时很开心。", "mailbox", "9"
            )
            with sqlite3.connect(service.db_path) as connection:
                row = connection.execute(
                    "SELECT content, context_card, cycle_id FROM xinchao_events"
                ).fetchone()
            self.assertIsNone(row[0])
            self.assertIn("事件卡", row[1])
            self.assertGreater(row[2], 0)
            self.assertEqual(result["cycle_id"], row[2])
            self.assertIn("事件卡", result["context_card"])

    async def test_api_failure_keeps_pending_event_without_losing_text(self):
        with tempfile.TemporaryDirectory() as root:
            service = XinchaoService(config(root))
            service.evaluator = FakeEvaluator(fail=True)
            result = await service.record_event("这条记忆已经保存，评估暂时失败。", "hold", "a")
            self.assertEqual(result["status"], "pending")
            with sqlite3.connect(service.db_path) as connection:
                row = connection.execute(
                    "SELECT status, content FROM xinchao_events"
                ).fetchone()
            self.assertEqual(row[0], "pending")
            self.assertIn("评估暂时失败", row[1])

    async def test_processed_event_does_not_duplicate_memory_body(self):
        with tempfile.TemporaryDirectory() as root:
            service = XinchaoService(config(root))
            service.evaluator = FakeEvaluator()
            await service.record_event("完整正文只属于记忆桶。", "hold", "a")
            with sqlite3.connect(service.db_path) as connection:
                content = connection.execute(
                    "SELECT content FROM xinchao_events WHERE status='applied'"
                ).fetchone()[0]
            self.assertIsNone(content)


class XinchaoServerHookTests(unittest.IsolatedAsyncioTestCase):
    def test_write_sidecar_identity_binds_request_to_actual_write(self):
        with patch.object(server, "_active_mcp_event_key", return_value="runtime-session:1\0" "2"):
            first = server._write_sidecar_event_id("第一封信", "mailbox", "74")
            repeated = server._write_sidecar_event_id("第一封信", "mailbox", "74")
            second = server._write_sidecar_event_id("第二封信", "mailbox", "75")
        self.assertEqual(first, repeated)
        self.assertNotEqual(first, second)

    async def test_task_failure_cannot_block_hormone_update(self):
        xinchao_hook = AsyncMock(return_value={"status": "applied"})
        task_hook = AsyncMock(side_effect=RuntimeError("task sidecar unavailable"))
        with (
            patch.object(server, "_active_mcp_event_key", return_value="runtime-session:1\0" "2"),
            patch.object(server, "_record_xinchao_event", new=xinchao_hook),
            patch.object(server.task_service, "process_event", new=task_hook),
        ):
            result = await server._record_write_sidecars(
                "这一封信写清了事件和感受。", "mailbox", "74"
            )
        xinchao_hook.assert_awaited_once()
        task_hook.assert_awaited_once()
        self.assertEqual(result["xinchao"]["status"], "applied")
        self.assertEqual(result["tasks"]["status"], "error")

    async def test_resonance_searches_memory_and_mailbox_for_deepseek(self):
        manager = type(
            "Manager",
            (),
            {
                "embedding_index": object(),
                "search": AsyncMock(
                    return_value=[
                        {
                            "id": "bucket-a",
                            "metadata": {"name": "以前一起等回家"},
                            "content": "旧记忆正文",
                            "matched_segment": "那次我也一直惦记她到家没有。",
                            "score": 86.0,
                            "semantic_score": 0.82,
                            "bm25_score": 0.44,
                        }
                    ]
                ),
            },
        )()
        mailbox_matches = [
            {
                "message_id": 7,
                "created_at": "2026-08-01T08:00:00+08:00",
                "message": "她上次出门时让我等她报平安。",
                "match_score": 0.79,
                "semantic_score": 0.76,
                "keyword_score": 0.25,
            }
        ]
        with (
            patch.object(server, "bucket_mgr", manager),
            patch.object(server, "mailbox_store", object()),
            patch.object(
                server,
                "search_mailbox",
                new=AsyncMock(return_value=mailbox_matches),
            ) as mailbox_search,
        ):
            result = await server._xinchao_memory_resonance_provider(
                {"pipes": {"想靠近": 0.8, "惦记": 0.7}},
                [{"context_card": "她说要出门，之后还没回来。"}],
            )

        self.assertEqual([item["source"] for item in result], ["memory", "mailbox"])
        manager.search.assert_awaited_once()
        mailbox_search.assert_awaited_once()
        self.assertIn("出门", manager.search.await_args.args[0])
        self.assertFalse(manager.search.await_args.kwargs["record_feedback"])
        self.assertIn("出门", mailbox_search.await_args.args[2])

    async def test_grow_bucket_and_mailbox_is_one_event(self):
        analysis = {
            "domain": ["日常生活"],
            "tags": ["日常生活"],
            "valence": 0.5,
            "arousal": 0.4,
            "suggested_name": "测试",
        }
        hook = AsyncMock(return_value={"status": "applied"})
        with (
            patch.object(server.decay_engine, "ensure_started", new=AsyncMock()),
            patch.object(server.dehydrator, "analyze", new=AsyncMock(return_value=analysis)),
            patch.object(server, "_check_conflicts", new=AsyncMock(return_value=[])),
            patch.object(server, "_append_or_create", new=AsyncMock(return_value=("bucket-a", False))),
            patch.object(server, "_store_grow_message", new=AsyncMock(return_value="\n留言已存")),
            patch.object(server, "_record_xinchao_event", new=hook),
        ):
            result = await server.grow("我记下事件和感受。", message="写给下个窗口")
        self.assertIn("留言已存", result)
        hook.assert_awaited_once()
        self.assertEqual(hook.await_args.args[1], "grow")
        self.assertIn("我记下事件和感受。", hook.await_args.args[0])
        self.assertIn("写给下个窗口", hook.await_args.args[0])

    async def test_standalone_mailbox_write_is_one_event(self):
        store = type(
            "Mailbox",
            (),
            {
                "add": AsyncMock(
                    return_value={"message_id": 7, "created_at": "2026-08-01T08:00:00+08:00"}
                )
            },
        )()
        hook = AsyncMock(return_value={"status": "applied"})
        with (
            patch.object(server, "mailbox_store", store),
            patch.object(server, "_record_xinchao_event", new=hook),
        ):
            result = await server.mailbox(message="我写清了事情和当时的感受。")
        self.assertIn("#7", result)
        hook.assert_awaited_once()
        self.assertEqual(hook.await_args.args[1], "mailbox")

    async def test_metadata_edit_does_not_trigger_but_trace_append_does(self):
        manager = type(
            "Manager",
            (),
            {
                "get": AsyncMock(
                    return_value={
                        "id": "bucket-a",
                        "content": "旧正文",
                        "metadata": {"sealed": False, "importance": 5},
                    }
                ),
                "update": AsyncMock(return_value=True),
            },
        )()
        hook = AsyncMock(return_value={"status": "applied"})
        with (
            patch.object(server, "bucket_mgr", manager),
            patch.object(server, "_record_xinchao_event", new=hook),
        ):
            await server.trace("bucket-a", importance=6)
            hook.assert_not_awaited()
            await server.trace("bucket-a", content="我追加了新的事件和感受。", append=True)
        hook.assert_awaited_once()
        self.assertEqual(hook.await_args.args[1], "trace_append")


if __name__ == "__main__":
    unittest.main()
