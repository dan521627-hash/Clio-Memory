"""Read-only planning for thematic memory digestion."""

import asyncio
import logging
import re
from datetime import datetime, timezone

from utils import beijing_now


logger = logging.getLogger("ombre_brain.digestion")

_TODO_RE = re.compile(r"待办|TODO|未完成|未完结|需要确认", re.IGNORECASE)
_WIKILINK_RE = re.compile(r"\[\[([^\]]+)\]\]")


class DigestionPlanner:
    """Build human-readable proposals without writing or moving any bucket."""

    def __init__(self, config: dict, bucket_mgr):
        settings = config.get("digestion", {})
        self.enabled = bool(settings.get("enabled", True))
        self.report_only = True
        self.interval_hours = max(1, int(settings.get("interval_hours", 168)))
        self.inactivity_days = max(1, int(settings.get("inactivity_days", 45)))
        self.max_importance = max(
            1, min(10, int(settings.get("max_importance", 4)))
        )
        self.max_activation_count = max(
            1, int(settings.get("max_activation_count", 2))
        )
        self.group_threshold = max(
            0.0,
            min(1.0, float(settings.get("group_similarity_threshold", 0.84))),
        )
        self.min_group_size = max(2, int(settings.get("min_group_size", 2)))
        self.bucket_mgr = bucket_mgr
        self.latest_report = None

    @staticmethod
    def _parse_time(value) -> datetime | None:
        try:
            parsed = datetime.fromisoformat(str(value))
        except (TypeError, ValueError):
            return None
        if parsed.tzinfo is None:
            return parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)

    @staticmethod
    def _has_open_todo(bucket: dict) -> bool:
        metadata = bucket.get("metadata", {})
        if metadata.get("resolved", False):
            return False
        searchable = " ".join(
            [
                *map(str, metadata.get("domain", [])),
                *map(str, metadata.get("tags", [])),
                str(bucket.get("content", "")),
            ]
        )
        return bool(_TODO_RE.search(searchable))

    def _eligibility_reason(self, bucket: dict, now: datetime) -> str | None:
        metadata = bucket.get("metadata", {})
        if metadata.get("type") != "dynamic":
            return "不是动态桶"
        if metadata.get("sealed", False):
            return "已封存"
        if metadata.get("pinned", False) or metadata.get("protected", False):
            return "已钉选或保护"
        if metadata.get("ai_feeling", False):
            return "感受类记忆"
        if metadata.get("trigger_date") and not metadata.get(
            "trigger_processed", False
        ):
            return "仍有未处理触发日期"
        if self._has_open_todo(bucket):
            return "仍含未完结待办"
        if int(metadata.get("importance", 5)) > self.max_importance:
            return "重要度高于演习门槛"
        if int(metadata.get("activation_count", 1)) > self.max_activation_count:
            return "访问次数高于演习门槛"
        last_active = self._parse_time(
            metadata.get("last_active") or metadata.get("created")
        )
        if not last_active:
            return "时间字段无法确认"
        age_days = (now - last_active).total_seconds() / 86400
        if age_days < self.inactivity_days:
            return "尚未长期闲置"
        return None

    @staticmethod
    def _components(
        bucket_ids: list[str],
        scores: dict[tuple[str, str], float],
        threshold: float,
    ) -> list[list[str]]:
        # Complete-link grouping: every pair inside a proposed group must pass
        # the threshold. This prevents A~B and B~C from dragging unrelated A/C
        # into one digestion proposal.
        groups = []
        for bucket_id in sorted(bucket_ids):
            for group in groups:
                if all(
                    scores.get(tuple(sorted((bucket_id, member_id))), -1.0)
                    >= threshold
                    for member_id in group
                ):
                    group.append(bucket_id)
                    break
            else:
                groups.append([bucket_id])
        return groups

    @staticmethod
    def _preview_text(bucket: dict, limit: int = 180) -> str:
        text = _WIKILINK_RE.sub(r"\1", str(bucket.get("content", "")))
        text = re.sub(r"\s+", " ", text).strip()
        if len(text) > limit:
            text = text[: limit - 1].rstrip() + "…"
        return text

    def _proposed_group(self, buckets: list[dict]) -> dict:
        names = [
            str(bucket.get("metadata", {}).get("name") or bucket["id"])
            for bucket in buckets
        ]
        domains = []
        for bucket in buckets:
            for domain in bucket.get("metadata", {}).get("domain", []):
                if domain not in domains:
                    domains.append(str(domain))
        title_seed = "、".join(domains[:2]) or names[0]
        summary_lines = [
            f"- {name}：{self._preview_text(bucket)}"
            for name, bucket in zip(names, buckets)
        ]
        return {
            "bucket_ids": [bucket["id"] for bucket in buckets],
            "bucket_names": names,
            "proposed_title": f"沉淀摘要：{title_seed}",
            "proposed_summary": "\n".join(summary_lines),
            "hypothetical_destination": "逐桶批准后才会归档；绝不删除",
        }

    async def preview(self, now: datetime | None = None) -> dict:
        reference = beijing_now(now)
        buckets = await self.bucket_mgr.list_all(
            include_archive=False, include_sealed=True
        )
        eligible = [
            bucket
            for bucket in buckets
            if self._eligibility_reason(bucket, reference) is None
        ]
        scores = await self.bucket_mgr.embedding_index.pairwise_scores(
            [bucket["id"] for bucket in eligible]
        )
        by_id = {bucket["id"]: bucket for bucket in eligible}
        components = self._components(list(by_id), scores, self.group_threshold)
        groups = [
            self._proposed_group([by_id[bucket_id] for bucket_id in component])
            for component in components
            if len(component) >= self.min_group_size
        ]
        grouped_ids = {
            bucket_id for group in groups for bucket_id in group["bucket_ids"]
        }
        ungrouped = [
            {
                "bucket_id": bucket["id"],
                "name": bucket.get("metadata", {}).get("name", bucket["id"]),
                "reason": "没有足够相近的同主题旧桶，保留原位",
            }
            for bucket in eligible
            if bucket["id"] not in grouped_ids
        ]
        report = {
            "generated_at": reference.isoformat(),
            "mode": "report_only",
            "scanned_count": len(buckets),
            "eligible_count": len(eligible),
            "planned_bucket_count": len(grouped_ids),
            "groups": groups,
            "ungrouped": ungrouped,
            "thresholds": {
                "inactivity_days": self.inactivity_days,
                "max_importance": self.max_importance,
                "max_activation_count": self.max_activation_count,
                "group_similarity": self.group_threshold,
            },
        }
        self.latest_report = report
        return report

    def render(self, report: dict) -> str:
        lines = [
            "=== 自动消化演习报告（绝不执行） ===",
            f"扫描桶数: {report['scanned_count']}",
            f"符合低重要度、长期未访问条件: {report['eligible_count']}",
            f"建议整理的原桶数: {report['planned_bucket_count']}",
        ]
        if not report["groups"]:
            lines.append("本轮不建议归档或合并任何桶。")
        for index, group in enumerate(report["groups"], start=1):
            lines.extend(
                [
                    "",
                    f"【建议组 {index}】",
                    "原桶: " + "、".join(group["bucket_names"]),
                    "拟生成: " + group["proposed_title"],
                    "拟摘要预览:",
                    group["proposed_summary"],
                    "原桶去向（仅假设）: " + group["hypothetical_destination"],
                    "风险: 摘要可能漏掉日期、数字、否定词或语境，必须逐桶人工确认。",
                ]
            )
        if report["ungrouped"]:
            lines.append("")
            lines.append("【符合旧低条件但本轮保留】")
            for item in report["ungrouped"]:
                lines.append(f"- {item['name']}：{item['reason']}")
        lines.extend(
            [
                "",
                "安全声明: 本报告没有新建、修改、归档、合并或删除任何桶。",
            ]
        )
        return "\n".join(lines)

    async def periodic_loop(self) -> None:
        if not self.enabled:
            return
        while True:
            try:
                report = await self.preview()
                logger.info(
                    "Digestion preview complete: scanned=%s eligible=%s planned=%s",
                    report["scanned_count"],
                    report["eligible_count"],
                    report["planned_bucket_count"],
                )
            except Exception as error:
                logger.error("Digestion preview failed: %s", error)
            await asyncio.sleep(self.interval_hours * 3600)
