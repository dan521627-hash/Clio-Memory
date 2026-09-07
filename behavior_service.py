"""Turn a settled darkflow stage into one safe, auditable external behavior."""

from __future__ import annotations

import asyncio
import logging
import os
import re
from datetime import datetime, time, timedelta

import httpx

from behavior_store import BehaviorStore
from utils import beijing_now, now_iso


logger = logging.getLogger("ombre_brain.behavior")


class BehaviorService:
    SILENCE_NUDGE_STAGE = 900_000

    def __init__(self, config: dict, evaluator):
        settings = config.get("behavior", {})
        self.enabled = bool(settings.get("enabled", True))
        self.mode = str(settings.get("mode", "rehearsal")).strip().lower()
        if self.mode not in {"rehearsal", "live"}:
            self.mode = "rehearsal"
        self.max_chars = max(40, min(500, int(settings.get("max_chars", 80))))
        self.minimum_delay_minutes = max(
            1, int(settings.get("minimum_delay_minutes", 10))
        )
        self.default_delay_minutes = max(
            self.minimum_delay_minutes,
            int(settings.get("default_delay_minutes", 30)),
        )
        self.maximum_delay_minutes = max(
            self.default_delay_minutes,
            int(settings.get("maximum_delay_minutes", 480)),
        )
        self.presence_retry_minutes = max(
            5, int(settings.get("presence_retry_minutes", 15))
        )
        self.follow_up_delay_minutes = max(
            1, min(1440, int(settings.get("follow_up_delay_minutes", 60)))
        )
        self.follow_up_hormone_threshold = max(
            0.0,
            min(1.0, float(settings.get("follow_up_hormone_threshold", 0.10))),
        )
        self.schedule_recheck_hormone_threshold = max(
            self.follow_up_hormone_threshold,
            min(1.0, float(settings.get("schedule_recheck_hormone_threshold", 0.35))),
        )
        self.burst_hormone_threshold = max(
            0.0,
            min(1.0, float(settings.get("burst_hormone_threshold", 0.75))),
        )
        self.max_messages = max(1, min(3, int(settings.get("max_messages", 3))))
        self.burst_interval_seconds = max(
            1, min(30, int(settings.get("burst_interval_seconds", 3)))
        )
        self.dedupe_threshold = max(
            0.45, min(0.95, float(settings.get("dedupe_threshold", 0.72)))
        )
        self.quiet_start = self._parse_clock(settings.get("quiet_start", "00:00"), time(0, 0))
        self.quiet_end = self._parse_clock(settings.get("quiet_end", "07:00"), time(7, 0))
        self.quiet_jitter_minutes = max(
            0, min(60, int(settings.get("quiet_jitter_minutes", 20)))
        )
        self.title = str(settings.get("title", "Clio"))[:60]
        self.base_url = str(
            os.environ.get("OMBRE_BARK_BASE_URL", "https://api.day.app")
        ).rstrip("/")
        self.device_key = os.environ.get("OMBRE_BARK_DEVICE_KEY", "").strip()
        self.evaluator = evaluator
        self.store = BehaviorStore(config)
        self.feedback_callback = None
        self.tendency_provider = None
        self.memory_resonance_provider = None

    @staticmethod
    def _parse_clock(value, fallback: time) -> time:
        try:
            hour, minute = str(value).strip().split(":", 1)
            return time(max(0, min(23, int(hour))), max(0, min(59, int(minute))))
        except (TypeError, ValueError):
            return fallback

    def _in_quiet_hours(self, moment: datetime) -> bool:
        current = moment.timetz().replace(tzinfo=None)
        if self.quiet_start == self.quiet_end:
            return False
        if self.quiet_start < self.quiet_end:
            return self.quiet_start <= current < self.quiet_end
        return current >= self.quiet_start or current < self.quiet_end

    def _quiet_release_at(self, moment: datetime, seed: int = 0) -> datetime:
        target = moment.replace(
            hour=self.quiet_end.hour,
            minute=self.quiet_end.minute,
            second=0,
            microsecond=0,
        )
        current = moment.timetz().replace(tzinfo=None)
        if self.quiet_start > self.quiet_end and current >= self.quiet_start:
            target += timedelta(days=1)
        elif target <= moment:
            target += timedelta(days=1)
        jitter = 0
        if self.quiet_jitter_minutes:
            jitter = abs(int(seed)) % (self.quiet_jitter_minutes + 1)
        return target + timedelta(minutes=jitter)

    def set_feedback_callback(self, callback) -> None:
        self.feedback_callback = callback

    def set_tendency_provider(self, callback) -> None:
        """Attach the read-only 性格轨迹 context used by behavior decisions."""
        self.tendency_provider = callback

    def set_memory_resonance_provider(self, callback) -> None:
        """Attach bounded historical recall for an actual push judgment."""
        self.memory_resonance_provider = callback

    async def _tendency_context(self) -> dict:
        if self.tendency_provider is None:
            return {}
        try:
            result = self.tendency_provider()
            if hasattr(result, "__await__"):
                result = await result
            return result if isinstance(result, dict) else {}
        except Exception as error:
            logger.warning("Behavior tendency context unavailable: %s", error)
            return {}

    async def _apply_sent_feedback(
        self, cycle_id: int, content: str, decision: dict
    ) -> dict:
        if self.feedback_callback is None:
            return {"status": "disabled"}
        try:
            return await self.feedback_callback(
                int(cycle_id), content, decision.get("aftereffect", {})
            )
        except Exception as error:
            logger.warning("Behavior feedback failed after delivery: %s", error)
            return {"status": "failed", "error": error.__class__.__name__}

    async def _remember_sent_fingerprint(self, content: str, decision: dict) -> None:
        try:
            await self.store.remember_fingerprint(
                content, decision.get("_expression_intent", "")
            )
        except Exception as error:
            logger.warning("Behavior dedupe fingerprint could not be saved: %s", error)

    @staticmethod
    def _event_text(contexts: list[dict]) -> str:
        return "\n".join(
            str(item.get("context_card") or item.get("event_summary") or "")
            for item in contexts
        )

    @staticmethod
    def _has_unresolved_follow_up(contexts: list[dict]) -> bool:
        """Recognize a real-world event that naturally needs a later answer."""
        text = BehaviorService._event_text(contexts)
        if not text:
            return False
        closed = re.search(
            r"(已经|刚刚)?(到家|回家|回来|平安到达|考完|面试完|看完医生|检查完|办完|结束了)",
            text,
        )
        if closed:
            return False
        return bool(
            re.search(
                r"(要|准备|打算|马上|一会儿|待会儿|等下|正在|刚)(出去|出门|出发|上路)"
                r"|在路上|去(医院|体检|考试|面试|办事|旅行|出差)"
                r"|到了?再说|到了?告诉|回来再说|结束后告诉",
                text,
            )
        )

    @staticmethod
    def _has_closed_follow_up(contexts: list[dict]) -> bool:
        """Avoid rechecking events whose context explicitly says they ended."""
        text = BehaviorService._event_text(contexts)
        return bool(
            re.search(
                r"(已经|刚刚)?(到家|回家|回来|平安到达|考完|面试完|看完医生|检查完|办完|结束了)",
                text,
            )
        )

    @staticmethod
    def _hormone_drive(pipes: dict | None) -> tuple[str, float]:
        """Return the strongest actual pipe, without privileging an old subset.

        The value is only an auditable summary on the candidate.  DeepSeek still
        receives all pipes, but the summary must not make 43 of 48 states look
        invisible before that judgment starts.
        """
        values = {}
        for name, raw_value in (pipes or {}).items():
            try:
                values[str(name)] = max(0.0, min(1.0, float(raw_value)))
            except (TypeError, ValueError):
                continue
        return max(values.items(), key=lambda item: (item[1], item[0])) if values else ("", 0.0)

    @staticmethod
    def _top_pipe_drivers(pipes: dict | None, limit: int = 6) -> list[dict]:
        """Keep a human-readable snapshot while DeepSeek still receives every pipe."""
        ranked = []
        for name, raw_value in (pipes or {}).items():
            try:
                value = max(0.0, min(1.0, float(raw_value)))
            except (TypeError, ValueError):
                continue
            ranked.append({"name": str(name), "value": round(value, 4)})
        ranked.sort(key=lambda item: (-item["value"], item["name"]))
        return ranked[: max(1, min(12, int(limit)))]

    @staticmethod
    def _expression_intent(
        state: dict,
        memory_resonance: list[dict] | None,
        recent_intents: list[str] | None = None,
        excluded: set[str] | None = None,
    ) -> dict:
        """Choose a live expression angle without always following one top pipe."""
        pipes = state.get("pipes", {}) or {}
        # This is a *selection hint*, not a rigid state-to-message template.
        # Every pipe belongs to a readable angle, so every real state can affect
        # whether / how the later DeepSeek decision speaks.  The model still
        # sees all 48 raw values and may choose wait or skip.
        groups = {
            "靠近她": ("想靠近", "想黏着", "肌肤饥渴", "性欲"),
            "惦记与守候": ("想知道她在干嘛", "想得到回应", "期待", "责任"),
            "分享与喜悦": ("想分享", "好奇", "开心", "兴奋", "感动"),
            "关怀与修复": ("想照顾她", "想让她开心", "想修复关系", "安心", "满足", "放松"),
            "需要被理解": ("想被理解", "想被确认", "委屈", "失落", "孤独"),
            "低落与不安": ("难过", "不安", "害怕", "紧绷"),
            "诚实复盘": ("复盘", "自省", "权衡", "求证", "愧疚"),
            "思绪打转": ("反刍", "预演", "警觉", "专注"),
            "给自己留白": ("想暂时独处", "回避", "压抑", "无语", "疲惫"),
            "关系摩擦": ("醋", "生气", "取悦压力"),
            "日常能量": ("精力", "社交", "闲"),
        }
        recent = list(recent_intents or [])
        blocked = excluded or set()
        memory_hint = ""
        if memory_resonance:
            first = memory_resonance[0]
            memory_hint = str(
                first.get("summary") or first.get("snippet") or first.get("name") or ""
            )[:160]
        candidates = []
        for intent, names in groups.items():
            if intent in blocked:
                continue
            values = [max(0.0, min(1.0, float(pipes.get(name, 0.0)))) for name in names]
            score = (max(values) if values else 0.0) + sum(values) * 0.08
            score -= recent[:4].count(intent) * 0.16
            candidates.append((score, intent, names))
        candidates.sort(key=lambda item: (item[0], item[1]), reverse=True)
        _, intent, names = candidates[0] if candidates else (0.0, "自然开口", ())
        return {
            "name": intent,
            "source_pipes": [name for name in names if float(pipes.get(name, 0.0)) > 0],
            "memory_hint": memory_hint,
        }

    @property
    def configured(self) -> bool:
        return bool(self.device_key)

    @staticmethod
    def _clean_push_title(value: str) -> str:
        return re.sub(r"[\x00-\x1f\x7f]+", " ", str(value or "")).strip()[:60]

    async def push_title(self) -> str:
        stored = await self.store.get_meta("push_title", self.title)
        return self._clean_push_title(stored) or self.title or "Clio"

    async def set_push_title(self, value: str) -> str:
        title = self._clean_push_title(value)
        if not title:
            raise ValueError("推送署名不能为空")
        return await self.store.set_meta("push_title", title)

    async def _send_bark(self, content: str) -> None:
        payload = {
            "device_key": self.device_key,
            "title": await self.push_title(),
            "body": content,
            "group": "Clio",
            "level": "active",
        }
        async with httpx.AsyncClient(timeout=20.0) as client:
            response = await client.post(f"{self.base_url}/push", json=payload)
            response.raise_for_status()

    def _decision_messages(self, decision: dict, pipes: dict | None) -> list[str]:
        """Normalize legacy single text and the new bounded message burst."""
        raw_messages = decision.get("messages")
        if not isinstance(raw_messages, list):
            raw_messages = [decision.get("content", "")]
        messages = []
        for value in raw_messages:
            text = str(value or "").strip()[: self.max_chars]
            if text and text not in messages:
                messages.append(text)

        strongest = max(
            (max(0.0, min(1.0, float(value))) for value in (pipes or {}).values()),
            default=0.0,
        )
        allowed = self.max_messages if strongest >= self.burst_hormone_threshold else 1
        return messages[:allowed]

    @staticmethod
    def _has_detached_narration(messages: list[str]) -> bool:
        """Reject observer-style Bark copy before anything can be sent."""
        blocked = ("他", "她", "用户", "对方")
        return any(token in message for message in messages for token in blocked)

    async def _unique_decision(self, context: dict, state: dict) -> dict:
        memory_resonance = list(context.get("memory_resonance") or [])[:2]
        recent_intents = await self.store.recent_intents(limit=8)
        tendency_context = await self._tendency_context()
        intent = self._expression_intent(
            state, memory_resonance, recent_intents=recent_intents
        )
        request = {
            **context,
            "expression_intent": intent,
            "recent_expression_intents": recent_intents,
            "tendency_context": tendency_context,
        }
        decision = await self.evaluator.behavior_decision(**request)
        messages = self._decision_messages(decision, state.get("pipes", {}))
        if str(decision.get("action_type", "skip")).lower() != "message" or not messages:
            decision["_expression_intent"] = intent["name"]
            return decision

        if self._has_detached_narration(messages):
            retry_request = {
                **request,
                "retry_instruction": (
                    "上一版用了旁观者或第三人称口吻，不能发送。必须改成第一人称，由我直接对你说："
                    "说话者只用‘我’，收件人只用‘你’，不得出现‘他、她、用户、对方’。"
                ),
            }
            retried = await self.evaluator.behavior_decision(**retry_request)
            retry_messages = self._decision_messages(retried, state.get("pipes", {}))
            if (
                str(retried.get("action_type", "skip")).lower() != "message"
                or not retry_messages
                or self._has_detached_narration(retry_messages)
            ):
                return {
                    "action_type": "skip",
                    "messages": [],
                    "content": "",
                    "aftereffect": {},
                    "reason": "两次文案都不是第一人称直说，本次不发送。",
                    "_expression_intent": intent["name"],
                    "_regenerated": True,
                }
            decision = retried
            messages = retry_messages
            decision["_regenerated"] = True

        duplicate = await self.store.similarity("\n".join(messages), limit=8)
        if float(duplicate.get("similarity", 0.0)) < self.dedupe_threshold:
            decision["_expression_intent"] = intent["name"]
            decision["_dedupe"] = duplicate
            return decision

        alternate = self._expression_intent(
            state,
            memory_resonance,
            recent_intents=recent_intents + [intent["name"]],
            excluded={intent["name"]},
        )
        retry_request = {
            **context,
            "expression_intent": alternate,
            "recent_expression_intents": recent_intents + [intent["name"]],
            "retry_instruction": (
                "上一版与最近8次真实推送过于相似。换一个表达角度、句式和具体落点；"
                "不要改写上一版，也不要为了不同而编造事实。"
            ),
        }
        retried = await self.evaluator.behavior_decision(**retry_request)
        retry_messages = self._decision_messages(retried, state.get("pipes", {}))
        retry_similarity = await self.store.similarity("\n".join(retry_messages), limit=8)
        if (
            str(retried.get("action_type", "skip")).lower() == "message"
            and retry_messages
            and not self._has_detached_narration(retry_messages)
            and float(retry_similarity.get("similarity", 0.0)) < self.dedupe_threshold
        ):
            retried["_expression_intent"] = alternate["name"]
            retried["_dedupe"] = retry_similarity
            retried["_regenerated"] = True
            return retried
        return {
            "action_type": "skip",
            "messages": [],
            "content": "",
            "aftereffect": {},
            "reason": "两次文案都与近48小时推送过于相似，本次不发送",
            "_expression_intent": alternate["name"],
            "_dedupe": retry_similarity,
            "_regenerated": True,
        }

    async def _send_messages(self, messages: list[str]) -> None:
        for index, message in enumerate(messages):
            if index:
                await asyncio.sleep(self.burst_interval_seconds)
            await self._send_bark(message)

    def _delay(self, value, default: int | None = None) -> int:
        try:
            minutes = int(float(value))
        except (TypeError, ValueError):
            minutes = default or self.default_delay_minutes
        return max(self.minimum_delay_minutes, min(self.maximum_delay_minutes, minutes))

    async def schedule_event(self, event: dict, state: dict) -> dict:
        """Keep a new write in the current cycle; do not schedule a push yet.

        A push is considered only after the user-defined 30-minute no-action
        threshold has actually been crossed.  Scheduling at write time created
        a second, legacy delivery path that could race the silence-entry path.
        """
        if not self.enabled or event.get("status") != "applied":
            return {"status": "ignored"}
        return {
            "status": "deferred",
            "reason": "新写入已并入本轮上下文；仅在30分钟无操作后进入静默时再判断推送",
        }

    async def process_due(
        self,
        state: dict,
        mailbox: dict | None,
        darkflow: dict | None = None,
    ) -> list[dict]:
        """Re-evaluate due event candidates independently from darkflow timing."""
        if not self.enabled:
            return []
        if not state.get("silence_eligible"):
            cycle_id = int(state.get("cycle_id") or 0)
            if cycle_id > 0:
                await self.store.cancel_cycle(cycle_id)
            return []
        if state.get("interaction_phase") == "silence":
            return []
        moment = beijing_now()
        candidates = await self.store.due_candidates(
            moment.isoformat(timespec="seconds"), limit=3
        )
        results = []
        for candidate in candidates:
            candidate_id = int(candidate["candidate_id"])
            decision_context = dict(candidate.get("decision_context") or {})
            if decision_context.get("phase") != "silence_entry":
                await self.store.update_candidate(
                    candidate_id,
                    "cancelled",
                    "旧写入时预排队已停用；只在真实静默入口判断推送",
                )
                results.append({"status": "cancelled", "candidate_id": candidate_id})
                continue
            if self._in_quiet_hours(moment):
                due_at = self._quiet_release_at(moment, candidate_id)
                await self.store.update_candidate(
                    candidate_id,
                    "waiting",
                    "北京时间夜间静默，天亮后重新判断",
                    due_at.isoformat(timespec="seconds"),
                )
                results.append(
                    {
                        "status": "quiet",
                        "candidate_id": candidate_id,
                        "resume_at": due_at.isoformat(timespec="seconds"),
                    }
                )
                continue
            if int(candidate["cycle_id"]) != int(state.get("cycle_id", -1)):
                await self.store.update_candidate(
                    candidate_id, "cancelled", "用户已经回来并开始了新一轮，旧候场取消"
                )
                results.append({"status": "cancelled", "candidate_id": candidate_id})
                continue
            if (
                decision_context.get("phase") != "silence_entry"
                and int(state.get("elapsed_seconds", 0))
                < self.minimum_delay_minutes * 60
            ):
                due_at = moment + timedelta(minutes=self.presence_retry_minutes)
                await self.store.update_candidate(
                    candidate_id,
                    "waiting",
                    "用户刚有活动，稍后再判断",
                    due_at.isoformat(timespec="seconds"),
                )
                results.append({"status": "waiting", "candidate_id": candidate_id})
                continue
            latest_contexts = list(state.get("event_contexts") or [])
            event_contexts = list(candidate.get("event_contexts") or [])
            seen = {str(item.get("context_card", "")) for item in event_contexts}
            event_contexts.extend(
                item for item in latest_contexts
                if str(item.get("context_card", "")) not in seen
            )
            try:
                memory_resonance = list(
                    (darkflow or {}).get("memory_resonance") or []
                )
                if self.memory_resonance_provider is not None:
                    try:
                        recalled = self.memory_resonance_provider(
                            state, event_contexts
                        )
                        if hasattr(recalled, "__await__"):
                            recalled = await recalled
                        seen_recall = {
                            (
                                str(item.get("source", "")),
                                str(
                                    item.get("message_id")
                                    or item.get("bucket_id")
                                    or item.get("excerpt", "")
                                ),
                            )
                            for item in memory_resonance
                        }
                        for item in recalled or []:
                            identity = (
                                str(item.get("source", "")),
                                str(
                                    item.get("message_id")
                                    or item.get("bucket_id")
                                    or item.get("excerpt", "")
                                ),
                            )
                            if identity not in seen_recall:
                                memory_resonance.append(item)
                                seen_recall.add(identity)
                    except Exception as error:
                        logger.warning(
                            "Push memory resonance unavailable: %s", error
                        )
                memory_resonance = memory_resonance[:4]
                current_hormone_name, current_hormone_drive = self._hormone_drive(
                    state.get("pipes", {})
                )
                decision = await self._unique_decision({
                    "pipes": state.get("pipes", {}),
                    "darkflow": str((darkflow or {}).get("content", "")),
                    "event_contexts": event_contexts[-8:],
                    "mailbox_context": mailbox,
                    "timing": {
                        "decided_at": moment.isoformat(timespec="seconds"),
                        "candidate_created_at": candidate.get("created_at"),
                        "due_at": candidate.get("due_at"),
                        "elapsed_seconds": state.get("elapsed_seconds", 0),
                        "sleep_stage": state.get("sleep_stage", ""),
                        "schedule_recheck": str(
                            candidate.get("decision_note", "")
                        ).startswith("情绪驱动较高，保留一次到点复核"),
                        "decision_phase": decision_context.get("phase", ""),
                        "silence_started_at": decision_context.get(
                            "silence_started_at", ""
                        ),
                    },
                    "required_follow_up": bool(candidate.get("follow_up_required")),
                    "hormone_context": {
                        "scheduled_name": candidate.get("hormone_name", ""),
                        "scheduled_drive": candidate.get("hormone_drive", 0),
                        "current_name": current_hormone_name,
                        "current_drive": current_hormone_drive,
                    },
                    "memory_resonance": memory_resonance,
                    "push_history": decision_context.get("previous_pushes", []),
                    "push_sequence": decision_context.get("push_sequence", 1),
                    "last_push_acknowledged": decision_context.get(
                        "last_push_acknowledged", False
                    ),
                    "emotion_drivers": decision_context.get("emotion_drivers", []),
                    "relationship_context": state.get("relationship", {}),
                }, state)
            except Exception as error:
                logger.warning("Due behavior decision failed: %s", error)
                due_at = moment + timedelta(minutes=self.presence_retry_minutes)
                await self.store.update_candidate(
                    candidate_id,
                    "waiting",
                    f"判断失败，稍后重试: {error.__class__.__name__}",
                    due_at.isoformat(timespec="seconds"),
                )
                results.append({"status": "waiting", "candidate_id": candidate_id})
                continue
            action_type = str(decision.get("action_type", "skip")).lower()
            reason = str(decision.get("reason", "")).strip()
            required_follow_up = bool(candidate.get("follow_up_required"))
            still_open = self._has_unresolved_follow_up(event_contexts)
            if (
                required_follow_up
                and still_open
                and current_hormone_drive >= self.follow_up_hormone_threshold
                and action_type == "skip"
                and int(candidate.get("attempts", 0)) < 2
            ):
                action_type = "wait"
                decision["wait_minutes"] = self.presence_retry_minutes
                reason = "仍有自然后续，但本次没有生成足够自然且不重复的说法，稍后再判断"
            if action_type == "wait" and int(candidate.get("attempts", 0)) < 2:
                wait_minutes = self._delay(
                    decision.get("wait_minutes"), self.presence_retry_minutes
                )
                due_at = moment + timedelta(minutes=wait_minutes)
                await self.store.update_candidate(
                    candidate_id,
                    "waiting",
                    reason or "DeepSeek判断现在还不是合适时机",
                    due_at.isoformat(timespec="seconds"),
                )
                results.append({"status": "waiting", "candidate_id": candidate_id})
                continue
            if action_type != "message":
                await self.store.update_candidate(
                    candidate_id, "skipped", reason or "到点后判断无需发送"
                )
                results.append({"status": "skipped", "candidate_id": candidate_id})
                continue
            messages = self._decision_messages(decision, state.get("pipes", {}))
            if not messages:
                await self.store.update_candidate(
                    candidate_id, "skipped", "到点判断没有生成可发送内容"
                )
                results.append({"status": "skipped", "candidate_id": candidate_id})
                continue
            stage_index = 1_000_000 + candidate_id
            existing = await self.store.get_for_stage(candidate["cycle_id"], stage_index)
            if existing:
                results.append({"status": "duplicate", "item": existing})
                continue
            status = "rehearsal"
            delivered_at = None
            error = ""
            if self.mode == "live":
                if not self.configured:
                    status = "held"
                    error = "Bark device key is not configured"
                else:
                    try:
                        await self._send_messages(messages)
                        status = "sent"
                        delivered_at = now_iso()
                    except Exception as send_error:
                        status = "failed"
                        error = f"{send_error.__class__.__name__}: {send_error}"
            item = await self.store.record(
                {
                    "cycle_id": candidate["cycle_id"],
                    "stage_index": stage_index,
                    "action_type": "message",
                    "content": "\n---\n".join(messages),
                    "status": status,
                    "delivered_at": delivered_at,
                    "error": error,
                    "context": {
                        "candidate_id": candidate_id,
                        "dominant": state.get("dominant", ""),
                        "dominant_value": state.get("dominant_value", 0),
                        "event_count": len(event_contexts),
                        "message_count": len(messages),
                        "reason": reason,
                        "trigger_summary": decision_context.get("trigger_summary", ""),
                        "emotion_drivers": decision_context.get("emotion_drivers", []),
                        "pipe_count": decision_context.get(
                            "pipe_count", len(state.get("pipes", {}) or {})
                        ),
                        "phase": decision_context.get("phase", "absence"),
                        "push_sequence": decision_context.get("push_sequence", 1),
                    },
                }
            )
            if status == "sent":
                await self._remember_sent_fingerprint("\n".join(messages), decision)
                await self._apply_sent_feedback(
                    int(candidate["cycle_id"]), "\n".join(messages), decision
                )
            await self.store.update_candidate(candidate_id, status, reason)
            if status in {"sent", "rehearsal"}:
                await self.store.cancel_cycle(candidate["cycle_id"], candidate_id)
            results.append({"status": status, "item": item})
        return results

    async def process(self, darkflow: dict | None, state: dict, mailbox: dict | None) -> dict:
        if not self.enabled or not darkflow:
            return {"status": "disabled" if not self.enabled else "idle"}
        cycle_id = int(darkflow.get("cycle_id", 0))
        stage_index = int(darkflow.get("stage_index", 0))
        moment = beijing_now()
        if self._in_quiet_hours(moment):
            return {
                "status": "quiet",
                "resume_at": self._quiet_release_at(moment, cycle_id + stage_index).isoformat(
                    timespec="seconds"
                ),
            }
        presence_only = (
            int(darkflow.get("event_count", 0)) == 0
            and not darkflow.get("mailbox_message_id")
        )
        phase = str(darkflow.get("phase") or "absence")
        if phase == "silence":
            return {"status": "ignored", "reason": "silence uses an isolated nudge"}
        existing = await self.store.get_for_stage(cycle_id, stage_index)
        if existing:
            return {"status": "duplicate", "item": existing}
        try:
            decision = await self._unique_decision({
                "pipes": state.get("pipes", {}),
                "darkflow": str(darkflow.get("content", "")),
                "event_contexts": darkflow.get("contexts", []),
                "mailbox_context": None if presence_only else mailbox,
                "timing": {
                    "decided_at": now_iso(),
                    "absence_started_at": darkflow.get("absence_started_at"),
                    "elapsed_seconds": darkflow.get("elapsed_seconds", 0),
                    "sleep_stage": darkflow.get("sleep_stage", ""),
                    "presence_only": presence_only,
                    "first_silence_nudge": bool(darkflow.get("first_silence_nudge"))
                    or (presence_only and stage_index == 1),
                    "interaction_phase": phase,
                },
                "memory_resonance": list(darkflow.get("memory_resonance") or []),
                "relationship_context": state.get("relationship", {}),
            }, state)
            messages = self._decision_messages(decision, state.get("pipes", {}))
            content = "\n---\n".join(messages)
            action_type = str(decision.get("action_type", "skip")).strip().lower()
            if action_type not in {"message", "wait", "skip"}:
                action_type = "skip"
            if action_type != "message":
                item = await self.store.record(
                    {
                        "cycle_id": cycle_id,
                        "stage_index": stage_index,
                        "action_type": action_type,
                        "content": content,
                        "status": "skipped",
                        "context": {
                            "phase": phase,
                            "presence_only": presence_only,
                            "dominant": state.get("dominant", ""),
                            "dominant_value": state.get("dominant_value", 0),
                            "sleep_stage": darkflow.get("sleep_stage", ""),
                        "event_count": len(darkflow.get("contexts", [])),
                        "reason": str(decision.get("reason", ""))[:300],
                        "trigger_summary": str(
                            ((darkflow.get("contexts") or [{}])[-1]).get(
                                "context_card", ""
                            )
                        )[:300],
                        "emotion_drivers": self._top_pipe_drivers(
                            state.get("pipes", {})
                        ),
                        "pipe_count": len(state.get("pipes", {}) or {}),
                    },
                    }
                )
                return {"status": "skipped", "item": item}
            if not content:
                raise ValueError("empty behavior content")
            status = "rehearsal"
            delivered_at = None
            error = ""
            if self.mode == "live":
                if not self.configured:
                    status = "held"
                    error = "Bark device key is not configured"
                else:
                    try:
                        await self._send_messages(messages)
                        status = "sent"
                        delivered_at = now_iso()
                    except Exception as send_error:
                        status = "failed"
                        error = f"{send_error.__class__.__name__}: {send_error}"
            item = await self.store.record(
                {
                    "cycle_id": cycle_id,
                    "stage_index": stage_index,
                    "action_type": action_type,
                    "content": content,
                    "status": status,
                    "delivered_at": delivered_at,
                    "error": error,
                    "context": {
                        "phase": phase,
                        "presence_only": presence_only,
                        "dominant": state.get("dominant", ""),
                        "dominant_value": state.get("dominant_value", 0),
                        "sleep_stage": darkflow.get("sleep_stage", ""),
                        "event_count": len(darkflow.get("contexts", [])),
                        "message_count": len(messages),
                        "reason": str(decision.get("reason", ""))[:300],
                        "trigger_summary": str(
                            ((darkflow.get("contexts") or [{}])[-1]).get(
                                "context_card", ""
                            )
                        )[:300],
                        "emotion_drivers": self._top_pipe_drivers(
                            state.get("pipes", {})
                        ),
                        "pipe_count": len(state.get("pipes", {}) or {}),
                    },
                }
            )
            if status == "sent":
                await self._remember_sent_fingerprint("\n".join(messages), decision)
                await self._apply_sent_feedback(
                    cycle_id, "\n".join(messages), decision
                )
            return {"status": status, "item": item}
        except Exception as error:
            logger.warning("Behavior decision failed: %s", error)
            return {"status": "failed", "error": str(error)}

    async def process_silence_nudge(self, state: dict) -> dict:
        """Create a throttled push decision during one true silence cycle.

        Silence remains open after a push. A later decision is allowed after a
        quiet interval, whether or not the previous push was acknowledged. The
        acknowledgement itself never changes the xinchao cycle.
        """
        if not self.enabled:
            return {"status": "disabled"}
        if not state.get("silence_eligible"):
            return {
                "status": "waiting",
                "phase": "active",
                "reason": "mailbox_write_required",
            }
        if state.get("interaction_phase") != "absence":
            return {"status": "waiting", "phase": "active"}
        cycle_id = int(state.get("cycle_id") or 0)
        if cycle_id <= 0:
            return {"status": "idle"}
        progress = await self.store.silence_progress(cycle_id)
        existing = progress.get("latest_candidate")
        if existing and str(existing.get("status")) not in {"pending", "waiting"}:
            existing = None
        if existing:
            return {"status": "duplicate", "item": existing}

        latest_candidate = progress.get("latest_candidate") or {}
        last_attempt_at = str(latest_candidate.get("updated_at") or "")
        push_history = list(progress.get("push_history") or [])
        if push_history:
            last_push = push_history[-1]
            last_attempt_at = max(
                last_attempt_at,
                str(
                    last_push.get("delivered_at")
                    or last_push.get("decided_at")
                    or ""
                ),
            )
        if last_attempt_at:
            try:
                elapsed = (
                    beijing_now() - datetime.fromisoformat(last_attempt_at)
                ).total_seconds()
                last_attempt = datetime.fromisoformat(last_attempt_at)
            except (TypeError, ValueError):
                elapsed = self.follow_up_delay_minutes * 60
                last_attempt = beijing_now()
            if elapsed < self.follow_up_delay_minutes * 60:
                next_push_at = last_attempt + timedelta(
                    minutes=self.follow_up_delay_minutes
                )
                return {
                    "status": "waiting",
                    "phase": "absence",
                    "reason": "上一条推送后仍未达到下一次判断间隔",
                    "next_push_at": next_push_at.isoformat(timespec="seconds"),
                }

        # Clear any legacy candidate that may have been scheduled immediately
        # after a write.  The silence-entry decision below is the only push
        # decision family allowed for this interaction cycle.
        await self.store.cancel_cycle(cycle_id)

        moment = beijing_now()
        contexts = list(state.get("event_contexts") or [])
        trigger_summary = ""
        if contexts:
            latest = contexts[-1]
            trigger_summary = str(
                latest.get("context_card")
                or latest.get("event_summary")
                or latest.get("event_tag")
                or ""
            ).strip()
        hormone_name, hormone_drive = self._hormone_drive(state.get("pipes", {}))
        push_sequence = int(progress.get("candidate_count") or 0) + 1
        source_event_id = (
            -abs(cycle_id)
            if push_sequence == 1
            else -(abs(cycle_id) * 1_000_000 + push_sequence)
        )
        history_for_model = []
        for item in push_history:
            context = item.get("context") or {}
            history_for_model.append(
                {
                    "sequence": context.get("push_sequence") or len(history_for_model) + 1,
                    "sent_at": item.get("delivered_at") or item.get("decided_at"),
                    "acknowledged": bool(item.get("acknowledged_at")),
                    "acknowledged_at": item.get("acknowledged_at"),
                    "content": str(item.get("content") or "")[:240],
                }
            )
        candidate = await self.store.upsert_candidate(
            {
                "cycle_id": cycle_id,
                "source_event_id": source_event_id,
                "created_at": state.get("absence_started_at")
                or moment.isoformat(timespec="seconds"),
                "due_at": moment.isoformat(timespec="seconds"),
                "expires_at": (moment + timedelta(hours=8)).isoformat(
                    timespec="seconds"
                ),
                "status": "pending",
                "event_contexts": contexts,
                "follow_up_required": self._has_unresolved_follow_up(contexts),
                "hormone_name": hormone_name,
                "hormone_drive": hormone_drive,
                "decision_note": (
                    "已连续30分钟没有新操作，进入静默；"
                    "正在结合本轮写入与48项情绪判断是否推送"
                ),
                "decision_context": {
                    "phase": "silence_entry",
                    "silence_started_at": state.get("absence_started_at"),
                    "last_activity_at": state.get("last_presence_at")
                    or state.get("last_event_at"),
                    "trigger_summary": trigger_summary,
                    "emotion_drivers": self._top_pipe_drivers(
                        state.get("pipes", {})
                    ),
                    "pipe_count": len(state.get("pipes", {}) or {}),
                    "push_sequence": push_sequence,
                    "previous_pushes": history_for_model,
                    "last_push_acknowledged": bool(
                        history_for_model
                        and history_for_model[-1].get("acknowledged")
                    ),
                },
            }
        )
        return {"status": "pending", "item": candidate}
