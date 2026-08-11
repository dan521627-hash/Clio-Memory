"""Detect possible dated fact changes without writing confirmed facts."""

from __future__ import annotations

import json
import logging

from fact_timeline_store import FactTimelineStore
from utils import beijing_now


logger = logging.getLogger("ombre_brain.fact_timeline")


class FactTimelineService:
    """Use the configured evaluator to create human-reviewable candidates."""

    def __init__(
        self, config: dict, evaluator, store: FactTimelineStore, bucket_manager=None
    ):
        self.store = store
        self.evaluator = evaluator
        self.bucket_manager = bucket_manager
        self.enabled = bool(store.enabled and store.auto_detect)

    async def _current_facts(self) -> list[dict]:
        groups = await self.store.list_facts(limit=100)
        visible = []
        for group in groups:
            current = group.get("current") or {}
            if not current:
                continue
            if not await self._source_is_visible(current):
                continue
            visible.append(
                {
                    "fact": group.get("fact_label", ""),
                    "value": current.get("fact_value", ""),
                    "effective_date": current.get("effective_date", ""),
                }
            )
        return visible

    async def _source_is_visible(self, item: dict) -> bool:
        source_type = str(item.get("source_type", "bucket") or "bucket").lower()
        if source_type != "bucket" or self.bucket_manager is None:
            return True
        source_id = str(item.get("source_bucket_id", "") or item.get("source_ref", "")).strip()
        if not source_id:
            return False
        try:
            bucket = await self.bucket_manager.get(source_id)
        except Exception:
            return False
        metadata = bucket.get("metadata", {}) if bucket else {}
        return bool(
            bucket
            and not metadata.get("sealed")
            and str(metadata.get("type", "")).lower() != "archived"
        )

    async def _extract(self, content: str, current_facts: list[dict]) -> list[dict]:
        if not getattr(self.evaluator, "client", None):
            raise RuntimeError("DeepSeek 未配置，事实变化暂未识别。")
        context = {
            "today_beijing": beijing_now().date().isoformat(),
            "event": str(content or "").strip()[:12000],
            "current_facts": current_facts,
        }
        response = await self.evaluator.client.chat.completions.create(
            model=self.evaluator.model,
            messages=[
                {
                    "role": "system",
                    "content": (
                        "你只负责发现可进入事实时间线的候选变化，不得直接修改事实。"
                        "只抓明确、可核对、以后可能发生新旧变化的事实，例如日期、金额、地点、"
                        "关系状态、订阅状态、设备、长期偏好或固定设置。不要抓情绪、感想、比喻、"
                        "私密叙事、一次性动作或不确定猜测。若新事件与已有事实不同，previous_value"
                        "填写旧值；若是首次出现的稳定事实则留空。effective_date 使用事件明确给出的"
                        "日期；没有明确日期时使用 today_beijing。confidence 低于0.75的不要返回。"
                        "最多返回3条。只返回JSON：{\"candidates\":[{\"fact\":\"事实名称\","
                        "\"value\":\"新值\",\"previous_value\":\"旧值或空\","
                        "\"effective_date\":\"YYYY-MM-DD\",\"confidence\":0.0,"
                        "\"reason\":\"为什么认为发生变化\",\"evidence\":\"原文短句\"}]}。"
                        "没有明确事实就返回空数组。"
                    ),
                },
                {"role": "user", "content": json.dumps(context, ensure_ascii=False)},
            ],
            max_tokens=700,
            temperature=0.0,
            response_format={"type": "json_object"},
            extra_body={"thinking": {"type": "disabled"}},
        )
        raw = response.choices[0].message.content if response.choices else ""
        payload = self.evaluator._clean_json(str(raw or ""))
        candidates = payload.get("candidates", [])
        if not isinstance(candidates, list):
            return []
        return [item for item in candidates if isinstance(item, dict)][
            : self.store.max_candidates_per_write
        ]

    async def process_event(
        self,
        content: str,
        source_type: str,
        source_ref: str = "",
        external_event_id: str = "",
    ) -> dict:
        if not self.enabled or not str(content or "").strip():
            return {"status": "disabled", "candidates": []}
        event_key = str(external_event_id or "").strip()
        if not event_key:
            raise ValueError("事实识别必须有稳定的事件编号。")
        previous = await self.store.event(event_key)
        if previous and previous.get("status") == "applied":
            return {"status": "duplicate", "candidates": []}
        source_kind = "mailbox" if source_type == "mailbox" else "bucket"
        source_bucket_id = "" if source_kind == "mailbox" else str(source_ref or "").strip()
        if source_kind == "bucket" and not await self._source_is_visible(
            {"source_type": "bucket", "source_bucket_id": source_bucket_id}
        ):
            return {"status": "hidden_source", "candidates": []}
        normalized = " ".join(str(content).strip().split())
        try:
            extracted = await self._extract(normalized, await self._current_facts())
            saved = []
            for item in extracted:
                try:
                    confidence = float(item.get("confidence", 0))
                except (TypeError, ValueError):
                    confidence = 0.0
                if confidence < 0.75:
                    continue
                candidate = await self.store.save_candidate(
                    {
                        "fact": item.get("fact", ""),
                        "value": item.get("value", ""),
                        "previous_value": item.get("previous_value", ""),
                        "effective_date": item.get("effective_date", ""),
                        "confidence": confidence,
                        "reason": item.get("reason", ""),
                        "source_excerpt": item.get("evidence", "") or normalized[:1000],
                        "source_type": source_kind,
                        "source_ref": str(source_ref or ""),
                        "source_bucket_id": source_bucket_id,
                        "event_key": event_key,
                    }
                )
                saved.append(candidate)
            await self.store.save_event(
                event_key,
                source_kind,
                str(source_ref or ""),
                "applied",
                json.dumps(
                    [item.get("candidate_id") for item in saved], ensure_ascii=False
                ),
            )
            return {"status": "applied", "candidates": saved}
        except Exception as error:
            logger.warning("Fact detection failed after %s write: %s", source_type, error)
            await self.store.save_event(
                event_key, source_kind, str(source_ref or ""), "pending", error=str(error)
            )
            return {"status": "pending", "candidates": [], "error": str(error)}

    async def confirm_candidate(self, candidate_id: int) -> dict:
        candidate = await self.store.get_candidate(candidate_id)
        if not candidate:
            raise ValueError("没有找到这条待确认变化。")
        if candidate["status"] == "confirmed":
            return {"candidate": candidate, "version": None, "status": "unchanged"}
        if candidate["status"] != "pending":
            raise ValueError("这条变化已经被忽略，不能再确认。")
        if not await self._source_is_visible(candidate):
            raise ValueError("候选变化的来源记忆不可用或已封存。")
        version = await self.store.record(
            fact=candidate["fact_label"],
            value=candidate["proposed_value"],
            effective_date=candidate["effective_date"],
            source_bucket_id=candidate["source_bucket_id"],
            source_type=candidate["source_type"],
            source_ref=candidate["source_ref"],
            source_excerpt=candidate["source_excerpt"],
        )
        resolved = await self.store.resolve_candidate(candidate_id, "confirmed")
        return {"candidate": resolved, "version": version, "status": version["status"]}

    async def ignore_candidate(self, candidate_id: int) -> dict:
        return await self.store.resolve_candidate(candidate_id, "ignored")
