"""Permanent removal of a bucket from the live memory store and sidecars."""

from __future__ import annotations

import asyncio
import os
import sqlite3
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class PurgeRule:
    label: str
    database: Path
    count_sql: str
    delete_sql: str
    parameter_count: int = 1
    like_parameter: bool = False


class PermanentDeleteService:
    """Delete online copies linked to one bucket id without touching backups."""

    def __init__(self, config: dict):
        self.config = config
        self.data_root = Path(config["buckets_dir"]).resolve()

    def _database_path(self, setting: str, env_name: str, filename: str) -> Path:
        configured = self.config.get(setting, {}).get("db_path")
        return Path(
            configured or os.environ.get(env_name, self.data_root / filename)
        ).resolve()

    def _rules(self) -> list[PurgeRule]:
        history = self._database_path("history", "OMBRE_HISTORY_DB", "history.sqlite3")
        embeddings = self._database_path(
            "embeddings", "OMBRE_EMBEDDING_DB", "embeddings.sqlite3"
        )
        summaries = self._database_path(
            "summary_cache", "OMBRE_SUMMARY_CACHE_DB", "summaries.sqlite3"
        )
        relations = self._database_path(
            "relations", "OMBRE_RELATIONS_DB", "relations.sqlite3"
        )
        feedback = self._database_path(
            "retrieval_feedback",
            "OMBRE_RETRIEVAL_FEEDBACK_DB",
            "retrieval_feedback.sqlite3",
        )
        timeline = self._database_path(
            "fact_timeline", "OMBRE_FACT_TIMELINE_DB", "fact_timeline.sqlite3"
        )
        topics = self._database_path("topics", "OMBRE_TOPICS_DB", "topics.sqlite3")
        xinchao = self._database_path("xinchao", "OMBRE_XINCHAO_DB", "xinchao.sqlite3")
        behavior = self._database_path(
            "behavior", "OMBRE_BEHAVIOR_DB", "behavior.sqlite3"
        )
        return [
            PurgeRule("history_snapshots", history, "SELECT COUNT(*) FROM bucket_history WHERE bucket_id=?", "DELETE FROM bucket_history WHERE bucket_id=?"),
            PurgeRule("embedding", embeddings, "SELECT COUNT(*) FROM embeddings WHERE bucket_id=?", "DELETE FROM embeddings WHERE bucket_id=?"),
            PurgeRule("summary", summaries, "SELECT COUNT(*) FROM summary_cache WHERE bucket_id=?", "DELETE FROM summary_cache WHERE bucket_id=?"),
            PurgeRule("relations", relations, "SELECT COUNT(*) FROM relations WHERE left_bucket_id=? OR right_bucket_id=?", "DELETE FROM relations WHERE left_bucket_id=? OR right_bucket_id=?", parameter_count=2),
            PurgeRule("retrieval_feedback", feedback, "SELECT COUNT(*) FROM retrieval_feedback WHERE bucket_id=?", "DELETE FROM retrieval_feedback WHERE bucket_id=?"),
            PurgeRule("fact_timeline", timeline, "SELECT COUNT(*) FROM fact_versions WHERE source_bucket_id=?", "DELETE FROM fact_versions WHERE source_bucket_id=?"),
            PurgeRule("topic_assignment", topics, "SELECT COUNT(*) FROM topic_assignments WHERE bucket_id=?", "DELETE FROM topic_assignments WHERE bucket_id=?"),
            PurgeRule("topic_bulk_history", topics, "SELECT COUNT(*) FROM topic_bulk_changes WHERE bucket_id=?", "DELETE FROM topic_bulk_changes WHERE bucket_id=?"),
            PurgeRule("emotion_events", xinchao, "SELECT COUNT(*) FROM xinchao_events WHERE source_ref=?", "DELETE FROM xinchao_events WHERE source_ref=?"),
            PurgeRule("darkflow_context", xinchao, "SELECT COUNT(*) FROM xinchao_darkflow WHERE context_json LIKE ?", "DELETE FROM xinchao_darkflow WHERE context_json LIKE ?", like_parameter=True),
            PurgeRule("transition_context", xinchao, "SELECT COUNT(*) FROM xinchao_transitions WHERE details_json LIKE ?", "DELETE FROM xinchao_transitions WHERE details_json LIKE ?", like_parameter=True),
            PurgeRule("behavior_candidates", behavior, "SELECT COUNT(*) FROM behavior_candidates WHERE event_context_json LIKE ?", "DELETE FROM behavior_candidates WHERE event_context_json LIKE ?", like_parameter=True),
            PurgeRule("behavior_actions", behavior, "SELECT COUNT(*) FROM behavior_actions WHERE context_json LIKE ?", "DELETE FROM behavior_actions WHERE context_json LIKE ?", like_parameter=True),
        ]

    @staticmethod
    def _parameters(rule: PurgeRule, bucket_id: str) -> tuple[str, ...]:
        value = f"%{bucket_id}%" if rule.like_parameter else bucket_id
        return tuple(value for _ in range(rule.parameter_count))

    @staticmethod
    def _table_exists(connection: sqlite3.Connection, sql: str) -> bool:
        words = sql.split()
        try:
            table = words[words.index("FROM") + 1]
        except (ValueError, IndexError):
            return False
        row = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
        ).fetchone()
        return row is not None

    def _preview_sync(self, bucket_id: str) -> dict[str, int]:
        counts: dict[str, int] = {}
        for rule in self._rules():
            if not rule.database.exists():
                counts[rule.label] = 0
                continue
            with sqlite3.connect(rule.database, timeout=30) as connection:
                if not self._table_exists(connection, rule.count_sql):
                    counts[rule.label] = 0
                    continue
                row = connection.execute(
                    rule.count_sql, self._parameters(rule, bucket_id)
                ).fetchone()
                counts[rule.label] = int(row[0]) if row else 0
        return counts

    async def preview(self, bucket_id: str) -> dict[str, int]:
        return await asyncio.to_thread(self._preview_sync, str(bucket_id).strip())

    def _purge_sync(self, bucket_id: str) -> dict[str, int]:
        removed: dict[str, int] = {}
        for rule in self._rules():
            if not rule.database.exists():
                removed[rule.label] = 0
                continue
            with sqlite3.connect(rule.database, timeout=30) as connection:
                if not self._table_exists(connection, rule.delete_sql):
                    removed[rule.label] = 0
                    continue
                cursor = connection.execute(
                    rule.delete_sql, self._parameters(rule, bucket_id)
                )
                removed[rule.label] = max(0, int(cursor.rowcount))
        return removed

    async def purge(self, bucket_id: str) -> dict[str, int]:
        return await asyncio.to_thread(self._purge_sync, str(bucket_id).strip())
