"""Automatic extraction and retrieval for unfinished matters."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from datetime import timedelta
from difflib import SequenceMatcher

import numpy as np

from task_store import TaskStore
from utils import beijing_now, now_iso


logger = logging.getLogger("ombre_brain.tasks")


class TaskService:
    """Extract concrete tasks without making narrative writes depend on the model."""

    def __init__(self, config: dict, evaluator, embedding_index):
        settings = config.get("tasks", {})
        self.enabled = bool(settings.get("enabled", True))
        self.auto_extract = bool(settings.get("auto_extract", True))
        self.max_candidates = max(3, min(20, int(settings.get("max_candidates", 8))))
        self.semantic_threshold = max(
            0.5, min(0.98, float(settings.get("semantic_threshold", 0.78)))
        )
        self.exact_dedupe_minutes = max(
            1, min(1440, int(settings.get("exact_dedupe_minutes", 10)))
        )
        self.completed_retention_days = max(
            1, min(3650, int(settings.get("completed_retention_days", 3)))
        )
        self.retention_check_hours = max(
            1, min(168, int(settings.get("retention_check_hours", 6)))
        )
        self.store = TaskStore(config)
        self.evaluator = evaluator
        self.embedding_index = embedding_index

    async def _embed_item(self, item: dict) -> None:
        if not getattr(self.embedding_index, "enabled", False):
            return
        text = self.store.task_text(item)
        digest = self.store.content_hash(text)
        if item.get("embedding_hash") == digest:
            return
        try:
            vector = await self.embedding_index.embed_passage(text)
            await self.store.set_embedding(
                int(item["task_id"]), vector, self.embedding_index.model_name, digest
            )
        except Exception as error:
            logger.warning("Task embedding failed for #%s: %s", item.get("task_id"), error)

    @staticmethod
    def _keyword_score(query: str, item: dict) -> float:
        query_text = " ".join(str(query or "").lower().split())
        text = " ".join(TaskStore.task_text(item).lower().split())
        if not query_text or not text:
            return 0.0
        if query_text in text:
            return 1.0
        tokens = [token for token in query_text.replace("，", " ").split() if token]
        overlap = sum(1 for token in tokens if token in text) / max(1, len(tokens))
        fuzzy = SequenceMatcher(None, query_text, text[: max(len(query_text) * 4, 160)]).ratio()
        return max(overlap, fuzzy)

    async def search(
        self,
        query: str,
        *,
        status: str = "",
        limit: int = 10,
        include_closed: bool = True,
    ) -> list[dict]:
        items = await self.store.list(status=status, limit=500)
        if not include_closed and not status:
            items = [item for item in items if item["status"] == "open"]
        if not str(query or "").strip():
            return items[: max(1, min(100, int(limit)))]
        semantic_scores = {}
        if getattr(self.embedding_index, "enabled", False):
            try:
                query_vector = await self.embedding_index.embed_query(query)
                for task_id, vector in await self.store.vectors():
                    if len(vector) == len(query_vector):
                        semantic_scores[task_id] = float(np.dot(query_vector, vector))
            except Exception as error:
                logger.warning("Task semantic search unavailable: %s", error)
        scored = []
        for item in items:
            semantic = semantic_scores.get(int(item["task_id"]), 0.0)
            keyword = self._keyword_score(query, item)
            score = max(semantic, keyword * 0.94)
            if score >= 0.22:
                scored.append(
                    {
                        **item,
                        "match_score": round(score, 4),
                        "semantic_score": round(semantic, 4),
                        "keyword_score": round(keyword, 4),
                    }
                )
        scored.sort(
            key=lambda item: (
                float(item["match_score"]), int(item["importance"]), item["updated_at"]
            ),
            reverse=True,
        )
        return scored[: max(1, min(100, int(limit)))]

    async def _extract(self, content: str, candidates: list[dict]) -> list[dict]:
        if not self.evaluator.client:
            raise RuntimeError("DeepSeek 未配置，未竟事件保留为待处理")
        context = {
            "event": str(content).strip()[:12000],
            "existing_tasks": [
                {
                    "task_id": item["task_id"],
                    "title": item["title"],
                    "details": item["details"][:500],
                    "status": item["status"],
                    "importance": item["importance"],
                    "manual_updated_at": item.get("manual_updated_at"),
                }
                for item in candidates[: self.max_candidates]
            ],
        }
        response = await self.evaluator.client.chat.completions.create(
            model=self.evaluator.model,
            messages=[
                {
                    "role": "system",
                    "content": (
                        "你从一段真实事件中维护未竟事项，不总结记忆。只抓明确需要以后完成、等待结果、"
                        "承诺要做或仍需处理的事情。流水账、情绪、愿望、已经当场结束的动作不要建任务。"
                        "如果事件明确说明已有事项完成、取消或出现了一个新的后续任务，使用给出的 task_id。"
                        "不要因相似措辞重复建任务；同一件事只保留一条。人工修改过的状态优先，"
                        "只有事件明确写出后来又产生了新的需求时才可 reopen。importance 为1到5："
                        "1很低、2较低、3普通、4重要、5紧要。最多返回5个动作。"
                        "只返回JSON：{\"actions\":[{\"action\":\"create|complete|cancel|reopen\","
                        "\"task_id\":0,\"title\":\"简短事项\",\"details\":\"必要上下文\","
                        "\"importance\":3,\"evidence\":\"原文依据\"}]}。没有事项就返回空数组。"
                    ),
                },
                {"role": "user", "content": json.dumps(context, ensure_ascii=False)},
            ],
            max_tokens=700,
            temperature=0.1,
            response_format={"type": "json_object"},
            extra_body={"thinking": {"type": "disabled"}},
        )
        raw = response.choices[0].message.content if response.choices else ""
        payload = self.evaluator._clean_json(str(raw or ""))
        actions = payload.get("actions", [])
        return actions[:5] if isinstance(actions, list) else []

    async def _candidate_tasks(self, content: str) -> list[dict]:
        matches = await self.search(content, limit=self.max_candidates, include_closed=True)
        if len(matches) >= self.max_candidates:
            return matches
        recent = await self.store.list(limit=self.max_candidates * 2)
        seen = {int(item["task_id"]) for item in matches}
        matches.extend(item for item in recent if int(item["task_id"]) not in seen)
        return matches[: self.max_candidates]

    async def _apply_action(
        self,
        action: dict,
        *,
        source_type: str,
        source_ref: str,
        event_key: str,
        content: str,
    ) -> dict | None:
        kind = str(action.get("action", "")).strip().lower()
        evidence = str(action.get("evidence", "")).strip()[:800]
        if kind == "create":
            title = str(action.get("title", "")).strip()
            if not title:
                return None
            duplicates = await self.search(title, status="open", limit=1)
            if duplicates and float(duplicates[0].get("match_score", 0)) >= self.semantic_threshold:
                item = await self.store.update(
                    int(duplicates[0]["task_id"]),
                    source_type=source_type,
                    source_ref=source_ref,
                    source_event_id=event_key,
                    excerpt=evidence or content[:800],
                )
                return {"action": "linked", "task": item}
            try:
                importance = int(action.get("importance", 3))
            except (TypeError, ValueError):
                importance = 3
            item = await self.store.create(
                title=title,
                details=str(action.get("details", "")).strip(),
                importance=max(1, min(5, importance)),
                created_by="auto",
                source_type=source_type,
                source_ref=source_ref,
                source_event_id=event_key,
                excerpt=evidence or content[:800],
            )
            await self._embed_item(item)
            return {"action": "created", "task": item}
        if kind not in {"complete", "cancel", "reopen"}:
            return None
        try:
            task_id = int(action.get("task_id", 0))
        except (TypeError, ValueError):
            return None
        item = await self.store.get(task_id)
        if not item:
            return None
        target = {"complete": "completed", "cancel": "cancelled", "reopen": "open"}[kind]
        if item["status"] == target:
            return {"action": "unchanged", "task": item}
        if kind == "reopen" and not evidence:
            return None
        updated = await self.store.update(
            task_id,
            status=target,
            manual=False,
            source_type=source_type,
            source_ref=source_ref,
            source_event_id=event_key,
            excerpt=evidence or content[:800],
        )
        return {"action": kind, "task": updated}

    async def process_event(
        self,
        content: str,
        source_type: str,
        source_ref: str = "",
        external_event_id: str = "",
    ) -> dict:
        if not self.enabled or not self.auto_extract or not str(content or "").strip():
            return {"status": "disabled", "changes": []}
        normalized = " ".join(str(content).strip().split())
        content_hash = hashlib.sha256(normalized.encode("utf-8")).hexdigest()
        event_key = str(external_event_id or "").strip()
        if not event_key:
            event_key = f"{source_type}:{source_ref}:{content_hash}:{now_iso()[:16]}"
        previous = await self.store.event(event_key)
        if previous and previous.get("status") == "applied":
            return {"status": "duplicate", "changes": []}
        since = (beijing_now() - timedelta(minutes=self.exact_dedupe_minutes)).isoformat(timespec="seconds")
        if await self.store.recent_hash(content_hash, since):
            await self.store.save_event(
                event_key=event_key, content_hash=content_hash, source_type=source_type,
                source_ref=source_ref, status="applied", result_json='{"duplicate":true}',
            )
            return {"status": "duplicate", "changes": []}
        try:
            candidates = await self._candidate_tasks(normalized)
            actions = await self._extract(normalized, candidates)
            changes = []
            for action in actions:
                if not isinstance(action, dict):
                    continue
                change = await self._apply_action(
                    action,
                    source_type=source_type,
                    source_ref=source_ref,
                    event_key=event_key,
                    content=normalized,
                )
                if change:
                    changes.append(change)
            await self.store.save_event(
                event_key=event_key, content_hash=content_hash, source_type=source_type,
                source_ref=source_ref, status="applied",
                result_json=json.dumps(
                    [{"action": item["action"], "task_id": item["task"]["task_id"]} for item in changes],
                    ensure_ascii=False,
                ),
            )
            return {"status": "applied", "changes": changes}
        except Exception as error:
            logger.exception("Task extraction failed after %s write: %s", source_type, error)
            await self.store.save_event(
                event_key=event_key, content_hash=content_hash, source_type=source_type,
                source_ref=source_ref, status="pending", error=str(error),
            )
            return {"status": "pending", "changes": [], "error": str(error)}

    async def create_manual(
        self, title: str, details: str = "", importance: int = 3, source: str = "manager"
    ) -> dict:
        item = await self.store.create(
            title=title, details=details, importance=importance, created_by="manual",
            source_type=source, source_ref="", source_event_id="", excerpt=details,
        )
        await self._embed_item(item)
        return item

    async def update_manual(self, task_id: int, **changes) -> dict:
        item = await self.store.update(task_id, manual=True, **changes)
        if "title" in changes or "details" in changes:
            await self._embed_item(item)
        return item

    async def purge_expired_completed(self) -> dict:
        return await self.store.purge_completed(self.completed_retention_days)

    async def boot_snapshot(self, open_limit: int = 10, completed_limit: int = 5) -> dict:
        return {
            "open": await self.store.list(status="open", limit=open_limit),
            "completed": await self.store.pending_completions(completed_limit),
        }

    async def context(self, query: str, limit: int = 3) -> list[dict]:
        items = await self.search(query, status="open", limit=limit, include_closed=False)
        return [
            {
                "task_id": item["task_id"], "title": item["title"],
                "details": item["details"][:500], "importance": item["importance"],
                "relevance": item.get("match_score", 0.0),
            }
            for item in items
        ]
