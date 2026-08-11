import json
import logging
import re

from rapidfuzz import fuzz


logger = logging.getLogger("ombre_brain.conflicts")


CONFLICT_PROMPT = """你是记忆写入前的事实对账器。请比较一条新记忆与若干条高度相似的旧记忆。

只报告真正需要人工裁决的矛盾：
1. 必须是同一主体、同一属性或同一事件；
2. 日期、数字、状态、偏好、承诺或其他关键事实互不兼容；
3. 仅仅主题相似、信息新增、侧重点不同或可以同时成立，不算冲突；
4. 不猜测，不改写任何记忆，不判断哪一边正确；
5. 每个冲突要引用旧事实、新事实并用一句话说明矛盾点。

只输出 JSON：
{"conflicts":[{"old_bucket_id":"...","old_fact":"...","new_fact":"...","point":"..."}]}
没有冲突时输出：{"conflicts":[]}
"""


class ConflictDetector:
    def __init__(self, config: dict, bucket_manager, dehydrator):
        settings = config.get("conflict_detection", {})
        self.enabled = bool(settings.get("enabled", True))
        self.similarity_threshold = float(settings.get("similarity_threshold", 0.78))
        self.max_candidates = max(1, int(settings.get("max_candidates", 3)))
        self.max_conflicts = max(1, int(settings.get("max_conflicts", 5)))
        self.bucket_manager = bucket_manager
        self.dehydrator = dehydrator

    async def detect(self, new_content: str) -> list[dict]:
        if not self.enabled or not new_content or not new_content.strip():
            return []

        try:
            scores = await self.bucket_manager.embedding_index.query_scores(new_content)
        except Exception as exc:
            logger.warning("Conflict candidate search unavailable: %s", exc)
            return []
        if not scores:
            return []

        buckets = await self.bucket_manager.list_all(include_archive=False)
        candidates = []
        for bucket in buckets:
            similarity = scores.get(bucket["id"])
            if similarity is None or similarity < self.similarity_threshold:
                continue
            candidate = dict(bucket)
            candidate["similarity"] = float(similarity)
            candidates.append(candidate)

        candidates.sort(key=lambda item: item["similarity"], reverse=True)
        candidates = candidates[:self.max_candidates]
        if not candidates:
            return []

        conflicts = []
        if self.dehydrator.api_available:
            try:
                conflicts = await self._api_detect(new_content, candidates)
            except Exception as exc:
                logger.warning("API conflict comparison failed, using local fallback: %s", exc)
        validated = self._validate(conflicts, candidates)
        if validated:
            return validated[:self.max_conflicts]

        conflicts = self._local_detect(new_content, candidates)
        return self._validate(conflicts, candidates)[:self.max_conflicts]

    async def _api_detect(self, new_content: str, candidates: list[dict]) -> list[dict]:
        old_memories = [
            {
                "id": bucket["id"],
                "name": bucket.get("metadata", {}).get("name", bucket["id"]),
                "similarity": round(bucket["similarity"], 4),
                "content": bucket.get("content", "")[:1800],
            }
            for bucket in candidates
        ]
        payload = json.dumps(
            {"new_memory": new_content[:2000], "old_memories": old_memories},
            ensure_ascii=False,
        )
        response = await self.dehydrator.client.chat.completions.create(
            model=self.dehydrator.model,
            messages=[
                {"role": "system", "content": CONFLICT_PROMPT},
                {"role": "user", "content": payload},
            ],
            max_tokens=min(self.dehydrator.max_tokens, 800),
            temperature=0.0,
        )
        if not response.choices:
            return []
        raw = response.choices[0].message.content or ""
        cleaned = raw.strip()
        if cleaned.startswith("```"):
            cleaned = cleaned.split("\n", 1)[-1].rsplit("```", 1)[0]
        parsed = json.loads(cleaned)
        return parsed.get("conflicts", []) if isinstance(parsed, dict) else []

    def _validate(self, conflicts: list, candidates: list[dict]) -> list[dict]:
        candidate_map = {
            bucket["id"]: {
                "name": bucket.get("metadata", {}).get("name", bucket["id"]),
                "similarity": round(bucket["similarity"], 4),
            }
            for bucket in candidates
        }
        validated = []
        seen = set()
        for conflict in conflicts if isinstance(conflicts, list) else []:
            if not isinstance(conflict, dict):
                continue
            bucket_id = str(conflict.get("old_bucket_id", "")).strip()
            point = str(conflict.get("point", "")).strip()
            if bucket_id not in candidate_map or not point or (bucket_id, point) in seen:
                continue
            seen.add((bucket_id, point))
            validated.append(
                {
                    "old_bucket_id": bucket_id,
                    "old_bucket_name": candidate_map[bucket_id]["name"],
                    "similarity": candidate_map[bucket_id]["similarity"],
                    "old_fact": str(conflict.get("old_fact", "")).strip(),
                    "new_fact": str(conflict.get("new_fact", "")).strip(),
                    "point": point,
                }
            )
        return validated

    @staticmethod
    def _local_detect(new_content: str, candidates: list[dict]) -> list[dict]:
        conflicts = []
        new_clauses = ConflictDetector._clauses(new_content)
        for bucket in candidates:
            found = False
            for new_clause in new_clauses:
                for old_clause in ConflictDetector._clauses(bucket.get("content", "")):
                    point = (
                        ConflictDetector._numeric_conflict(old_clause, new_clause)
                        or ConflictDetector._polarity_conflict(old_clause, new_clause)
                    )
                    if not point:
                        continue
                    conflicts.append(
                        {
                            "old_bucket_id": bucket["id"],
                            "old_fact": old_clause,
                            "new_fact": new_clause,
                            "point": point,
                        }
                    )
                    found = True
                    break
                if found:
                    break
        return conflicts

    @staticmethod
    def _clauses(text: str) -> list[str]:
        return [
            clause.strip()
            for clause in re.split(r"[。！？；\n.!?;]+", text or "")
            if clause.strip()
        ]

    @staticmethod
    def _numeric_conflict(old_clause: str, new_clause: str) -> str:
        value_pattern = re.compile(
            r"(?:\d{4}[-/.年]\d{1,2}(?:[-/.月]\d{1,2}日?)?)"
            r"|(?:\d+(?:\.\d+)?\s*(?:元|万元|块|岁|年|月|日|天|小时|分钟|次|个|人|%|kg|公斤|斤|cm|厘米|GB|MB))",
            re.IGNORECASE,
        )
        old_values = value_pattern.findall(old_clause)
        new_values = value_pattern.findall(new_clause)
        if not old_values or not new_values or old_values == new_values:
            return ""
        old_context = value_pattern.sub("<值>", old_clause)
        new_context = value_pattern.sub("<值>", new_clause)
        if fuzz.ratio(old_context, new_context) < 88:
            return ""
        return f"同一事实的数值或日期不一致：旧为“{', '.join(old_values)}”，新为“{', '.join(new_values)}”"

    @staticmethod
    def _polarity_conflict(old_clause: str, new_clause: str) -> str:
        negation = re.compile(r"不|没|未|无|不是|没有|不会|不能|拒绝")
        old_negative = bool(negation.search(old_clause))
        new_negative = bool(negation.search(new_clause))
        if old_negative == new_negative:
            return ""
        old_base = negation.sub("", old_clause)
        new_base = negation.sub("", new_clause)
        if fuzz.ratio(old_base, new_base) < 92:
            return ""
        return "同一事实的肯定与否定表述互相冲突"
