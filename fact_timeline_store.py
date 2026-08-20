"""SQLite sidecar for dated versions of changing facts."""

import asyncio
import os
import re
import sqlite3
import uuid
from datetime import date
from pathlib import Path

from rapidfuzz import fuzz

from utils import now_iso


class FactTimelineStore:
    """Keep fact history outside Markdown memory buckets."""

    def __init__(self, config: dict):
        settings = config.get("fact_timeline", {})
        self.enabled = bool(settings.get("enabled", True))
        self.max_versions_per_response = max(
            1, int(settings.get("max_versions_per_response", 20))
        )
        self.auto_detect = bool(settings.get("auto_detect", True))
        self.max_candidates_per_write = max(
            1, min(5, int(settings.get("max_candidates_per_write", 3)))
        )
        self.db_path = settings.get("db_path") or os.environ.get(
            "OMBRE_FACT_TIMELINE_DB",
            os.path.join(config["buckets_dir"], "fact_timeline.sqlite3"),
        )
        if self.enabled:
            self._init_db()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.db_path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA synchronous=NORMAL")
        connection.execute("PRAGMA foreign_keys=ON")
        return connection

    def _init_db(self) -> None:
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS fact_versions (
                    version_id TEXT PRIMARY KEY,
                    fact_key TEXT NOT NULL,
                    fact_label TEXT NOT NULL,
                    fact_value TEXT NOT NULL,
                    effective_date TEXT NOT NULL,
                    valid_to TEXT,
                    is_current INTEGER NOT NULL DEFAULT 0,
                    source_bucket_id TEXT NOT NULL,
                    recorded_at TEXT NOT NULL,
                    CHECK (is_current IN (0, 1)),
                    UNIQUE (fact_key, effective_date)
                )
                """
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_fact_versions_key_date "
                "ON fact_versions(fact_key, effective_date)"
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_fact_versions_source "
                "ON fact_versions(source_bucket_id, fact_key)"
            )
            connection.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_fact_versions_current "
                "ON fact_versions(fact_key) WHERE is_current=1"
            )
            columns = {
                row["name"]
                for row in connection.execute("PRAGMA table_info(fact_versions)")
            }
            if "source_type" not in columns:
                connection.execute(
                    "ALTER TABLE fact_versions ADD COLUMN source_type TEXT NOT NULL DEFAULT 'bucket'"
                )
            if "source_ref" not in columns:
                connection.execute(
                    "ALTER TABLE fact_versions ADD COLUMN source_ref TEXT NOT NULL DEFAULT ''"
                )
            if "source_excerpt" not in columns:
                connection.execute(
                    "ALTER TABLE fact_versions ADD COLUMN source_excerpt TEXT NOT NULL DEFAULT ''"
                )
            connection.execute(
                "UPDATE fact_versions SET source_ref=source_bucket_id "
                "WHERE source_ref='' AND source_bucket_id<>''"
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS fact_candidates (
                    candidate_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    fact_key TEXT NOT NULL,
                    fact_label TEXT NOT NULL,
                    proposed_value TEXT NOT NULL,
                    previous_value TEXT NOT NULL DEFAULT '',
                    effective_date TEXT NOT NULL,
                    source_type TEXT NOT NULL,
                    source_ref TEXT NOT NULL DEFAULT '',
                    source_bucket_id TEXT NOT NULL DEFAULT '',
                    source_excerpt TEXT NOT NULL DEFAULT '',
                    confidence REAL NOT NULL DEFAULT 0,
                    reason TEXT NOT NULL DEFAULT '',
                    event_key TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending',
                    created_at TEXT NOT NULL,
                    resolved_at TEXT,
                    CHECK (status IN ('pending', 'confirmed', 'ignored')),
                    UNIQUE (event_key, fact_key, proposed_value, effective_date)
                )
                """
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_fact_candidates_status_created "
                "ON fact_candidates(status, created_at DESC)"
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS fact_detection_events (
                    event_key TEXT PRIMARY KEY,
                    source_type TEXT NOT NULL,
                    source_ref TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL,
                    result_json TEXT NOT NULL DEFAULT '',
                    error TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )

    @staticmethod
    def normalize_fact_key(value: str) -> tuple[str, str]:
        label = re.sub(r"\s+", " ", str(value or "").strip())
        if not label:
            raise ValueError("事实名称不能为空。")
        if len(label) > 120:
            raise ValueError("事实名称不能超过 120 个字符。")
        return label.casefold(), label

    @staticmethod
    def normalize_effective_date(value: str) -> str:
        text = str(value or "").strip()
        try:
            parsed = date.fromisoformat(text)
        except (TypeError, ValueError):
            raise ValueError("生效日期必须是有效的 YYYY-MM-DD。") from None
        if parsed.isoformat() != text:
            raise ValueError("生效日期必须是有效的 YYYY-MM-DD。")
        return text

    @staticmethod
    def normalize_value(value: str) -> str:
        text = str(value or "").strip()
        if not text:
            raise ValueError("事实内容不能为空。")
        if len(text) > 1000:
            raise ValueError("事实内容不能超过 1000 个字符。")
        return text

    def _record_sync(
        self,
        fact: str,
        value: str,
        effective_date: str,
        source_bucket_id: str = "",
        source_type: str = "bucket",
        source_ref: str = "",
        source_excerpt: str = "",
    ) -> dict:
        fact_key, fact_label = self.normalize_fact_key(fact)
        fact_value = self.normalize_value(value)
        effective = self.normalize_effective_date(effective_date)
        bucket_id = str(source_bucket_id or "").strip()
        source_kind = str(source_type or "bucket").strip().lower()
        if source_kind not in {"bucket", "mailbox", "manual"}:
            raise ValueError("事实来源类型无效。")
        source_reference = str(source_ref or "").strip() or bucket_id
        excerpt = str(source_excerpt or "").strip()[:1000]
        if source_kind == "bucket" and not bucket_id:
            raise ValueError("必须提供来源 bucket_id。")

        with self._connect() as connection:
            same_date = connection.execute(
                """
                SELECT * FROM fact_versions
                WHERE fact_key=? AND effective_date=?
                """,
                (fact_key, effective),
            ).fetchone()
            if same_date:
                if (
                    same_date["fact_value"] == fact_value
                    and same_date["source_bucket_id"] == bucket_id
                ):
                    result = dict(same_date)
                    result["status"] = "unchanged"
                    return result
                raise ValueError(
                    "同一事实在同一天已有不同记录，系统不会自动覆盖，请先人工核对。"
                )

            current = connection.execute(
                "SELECT * FROM fact_versions WHERE fact_key=? AND is_current=1",
                (fact_key,),
            ).fetchone()
            if current and current["fact_value"] == fact_value:
                result = dict(current)
                result["status"] = "unchanged"
                return result

            successor = connection.execute(
                """
                SELECT * FROM fact_versions
                WHERE fact_key=? AND effective_date>?
                ORDER BY effective_date ASC LIMIT 1
                """,
                (fact_key, effective),
            ).fetchone()
            predecessor = connection.execute(
                """
                SELECT * FROM fact_versions
                WHERE fact_key=? AND effective_date<?
                ORDER BY effective_date DESC LIMIT 1
                """,
                (fact_key, effective),
            ).fetchone()

            if predecessor:
                connection.execute(
                    "UPDATE fact_versions SET valid_to=? WHERE version_id=?",
                    (effective, predecessor["version_id"]),
                )

            is_current = 0 if successor else 1
            if is_current and current:
                connection.execute(
                    """
                    UPDATE fact_versions
                    SET is_current=0, valid_to=?
                    WHERE version_id=?
                    """,
                    (effective, current["version_id"]),
                )

            version_id = uuid.uuid4().hex[:16]
            recorded_at = now_iso()
            connection.execute(
                """
                INSERT INTO fact_versions (
                    version_id, fact_key, fact_label, fact_value,
                    effective_date, valid_to, is_current,
                    source_bucket_id, recorded_at, source_type,
                    source_ref, source_excerpt
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    version_id,
                    fact_key,
                    fact_label,
                    fact_value,
                    effective,
                    successor["effective_date"] if successor else None,
                    is_current,
                    bucket_id,
                    recorded_at,
                    source_kind,
                    source_reference,
                    excerpt,
                ),
            )
            row = connection.execute(
                "SELECT * FROM fact_versions WHERE version_id=?", (version_id,)
            ).fetchone()
        result = dict(row)
        result["status"] = "recorded"
        return result

    async def record(
        self,
        fact: str,
        value: str,
        effective_date: str,
        source_bucket_id: str = "",
        source_type: str = "bucket",
        source_ref: str = "",
        source_excerpt: str = "",
    ) -> dict:
        if not self.enabled:
            raise RuntimeError("事实时间线功能未启用。")
        return await asyncio.to_thread(
            self._record_sync,
            fact,
            value,
            effective_date,
            source_bucket_id,
            source_type,
            source_ref,
            source_excerpt,
        )

    def _save_candidate_sync(self, candidate: dict) -> dict:
        fact_key, fact_label = self.normalize_fact_key(candidate.get("fact", ""))
        proposed = self.normalize_value(candidate.get("value", ""))
        effective = self.normalize_effective_date(candidate.get("effective_date", ""))
        source_type = str(candidate.get("source_type", "") or "manual").strip().lower()
        if source_type not in {"bucket", "mailbox", "manual"}:
            source_type = "manual"
        event_key = str(candidate.get("event_key", "")).strip()
        if not event_key:
            raise ValueError("候选变化缺少事件编号。")
        try:
            confidence = max(0.0, min(1.0, float(candidate.get("confidence", 0))))
        except (TypeError, ValueError):
            confidence = 0.0
        values = (
            fact_key,
            fact_label,
            proposed,
            str(candidate.get("previous_value", "")).strip()[:1000],
            effective,
            source_type,
            str(candidate.get("source_ref", "")).strip()[:160],
            str(candidate.get("source_bucket_id", "")).strip()[:160],
            str(candidate.get("source_excerpt", "")).strip()[:1000],
            confidence,
            str(candidate.get("reason", "")).strip()[:500],
            event_key,
            now_iso(),
        )
        with self._connect() as connection:
            connection.execute(
                """
                INSERT OR IGNORE INTO fact_candidates (
                    fact_key, fact_label, proposed_value, previous_value,
                    effective_date, source_type, source_ref, source_bucket_id,
                    source_excerpt, confidence, reason, event_key, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                values,
            )
            row = connection.execute(
                """
                SELECT * FROM fact_candidates
                WHERE event_key=? AND fact_key=? AND proposed_value=? AND effective_date=?
                """,
                (event_key, fact_key, proposed, effective),
            ).fetchone()
        return dict(row)

    async def save_candidate(self, candidate: dict) -> dict:
        if not self.enabled:
            raise RuntimeError("事实时间线功能未启用。")
        return await asyncio.to_thread(self._save_candidate_sync, candidate)

    def _list_candidates_sync(self, status: str, limit: int) -> list[dict]:
        safe_status = str(status or "pending").strip().lower()
        if safe_status not in {"pending", "confirmed", "ignored", "all"}:
            raise ValueError("候选状态无效。")
        where = "" if safe_status == "all" else "WHERE status=?"
        params = () if safe_status == "all" else (safe_status,)
        with self._connect() as connection:
            rows = connection.execute(
                f"SELECT * FROM fact_candidates {where} ORDER BY created_at DESC LIMIT ?",
                (*params, max(1, min(500, int(limit)))),
            ).fetchall()
        return [dict(row) for row in rows]

    async def list_candidates(self, status: str = "pending", limit: int = 100) -> list[dict]:
        if not self.enabled:
            return []
        return await asyncio.to_thread(self._list_candidates_sync, status, limit)

    def _get_candidate_sync(self, candidate_id: int) -> dict | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM fact_candidates WHERE candidate_id=?", (int(candidate_id),)
            ).fetchone()
        return dict(row) if row else None

    async def get_candidate(self, candidate_id: int) -> dict | None:
        if not self.enabled:
            return None
        return await asyncio.to_thread(self._get_candidate_sync, candidate_id)

    def _resolve_candidate_sync(self, candidate_id: int, status: str) -> dict:
        if status not in {"confirmed", "ignored"}:
            raise ValueError("候选只能确认或忽略。")
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM fact_candidates WHERE candidate_id=?", (int(candidate_id),)
            ).fetchone()
            if not row:
                raise ValueError("没有找到这条待确认变化。")
            if row["status"] != "pending":
                return dict(row)
            connection.execute(
                "UPDATE fact_candidates SET status=?, resolved_at=? WHERE candidate_id=?",
                (status, now_iso(), int(candidate_id)),
            )
            updated = connection.execute(
                "SELECT * FROM fact_candidates WHERE candidate_id=?", (int(candidate_id),)
            ).fetchone()
        return dict(updated)

    async def resolve_candidate(self, candidate_id: int, status: str) -> dict:
        return await asyncio.to_thread(self._resolve_candidate_sync, candidate_id, status)

    def _event_sync(self, event_key: str) -> dict | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM fact_detection_events WHERE event_key=?", (event_key,)
            ).fetchone()
        return dict(row) if row else None

    async def event(self, event_key: str) -> dict | None:
        if not self.enabled:
            return None
        return await asyncio.to_thread(self._event_sync, event_key)

    def _save_event_sync(
        self, event_key: str, source_type: str, source_ref: str,
        status: str, result_json: str = "", error: str = "",
    ) -> None:
        timestamp = now_iso()
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO fact_detection_events (
                    event_key, source_type, source_ref, status,
                    result_json, error, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(event_key) DO UPDATE SET
                    status=excluded.status, result_json=excluded.result_json,
                    error=excluded.error, updated_at=excluded.updated_at
                """,
                (event_key, source_type, source_ref, status, result_json, error, timestamp, timestamp),
            )

    async def save_event(
        self, event_key: str, source_type: str, source_ref: str,
        status: str, result_json: str = "", error: str = "",
    ) -> None:
        await asyncio.to_thread(
            self._save_event_sync, event_key, source_type, source_ref,
            status, result_json, error,
        )

    def _versions_sync(self, fact: str) -> list[dict]:
        fact_key, _ = self.normalize_fact_key(fact)
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM (
                    SELECT * FROM fact_versions
                    WHERE fact_key=?
                    ORDER BY effective_date DESC, recorded_at DESC
                    LIMIT ?
                )
                ORDER BY effective_date ASC, recorded_at ASC
                """,
                (fact_key, self.max_versions_per_response),
            ).fetchall()
        return [dict(row) for row in rows]

    async def versions(self, fact: str) -> list[dict]:
        if not self.enabled:
            return []
        return await asyncio.to_thread(self._versions_sync, fact)

    def _versions_for_bucket_sync(self, bucket_id: str) -> list[dict]:
        with self._connect() as connection:
            keys = connection.execute(
                """
                SELECT DISTINCT fact_key FROM fact_versions
                WHERE source_bucket_id=?
                """,
                (bucket_id,),
            ).fetchall()
            if not keys:
                return []
            placeholders = ",".join("?" for _ in keys)
            rows = connection.execute(
                f"""
                SELECT * FROM (
                    SELECT * FROM fact_versions
                    WHERE fact_key IN ({placeholders})
                    ORDER BY effective_date DESC, recorded_at DESC
                    LIMIT ?
                )
                ORDER BY fact_key, effective_date ASC, recorded_at ASC
                """,
                (*tuple(row["fact_key"] for row in keys), self.max_versions_per_response),
            ).fetchall()
        return [dict(row) for row in rows]

    async def versions_for_bucket(self, bucket_id: str) -> list[dict]:
        if not self.enabled:
            return []
        return await asyncio.to_thread(self._versions_for_bucket_sync, bucket_id)

    def _related_buckets_sync(self, bucket_id: str, limit: int) -> list[dict]:
        """Find other bucket sources that describe versions of the same fact."""
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT DISTINCT
                    other.source_bucket_id AS bucket_id,
                    other.fact_key,
                    other.fact_label,
                    other.effective_date,
                    other.is_current
                FROM fact_versions AS source
                JOIN fact_versions AS other ON other.fact_key=source.fact_key
                WHERE source.source_type='bucket'
                  AND source.source_bucket_id=?
                  AND other.source_type='bucket'
                  AND other.source_bucket_id<>''
                  AND other.source_bucket_id<>?
                ORDER BY other.is_current DESC, other.effective_date DESC
                LIMIT ?
                """,
                (bucket_id, bucket_id, max(1, min(50, int(limit)))),
            ).fetchall()
        return [dict(row) for row in rows]

    async def related_buckets(self, bucket_id: str, limit: int = 10) -> list[dict]:
        if not self.enabled:
            return []
        return await asyncio.to_thread(self._related_buckets_sync, bucket_id, limit)

    def _list_facts_sync(self, search: str, limit: int) -> list[dict]:
        query = re.sub(r"\s+", " ", str(search or "").strip())
        safe_limit = max(1, min(200, int(limit)))
        with self._connect() as connection:
            keys = connection.execute(
                """
                SELECT fact_key, MAX(fact_label) AS fact_label,
                       MAX(recorded_at) AS latest_recorded_at
                FROM fact_versions
                GROUP BY fact_key
                ORDER BY latest_recorded_at DESC, fact_label ASC
                """,
            ).fetchall()
            groups = []
            for key in keys:
                rows = connection.execute(
                    """
                    SELECT * FROM (
                        SELECT * FROM fact_versions
                        WHERE fact_key=?
                        ORDER BY effective_date DESC, recorded_at DESC
                        LIMIT ?
                    )
                    ORDER BY effective_date ASC, recorded_at ASC
                    """,
                    (key["fact_key"], self.max_versions_per_response),
                ).fetchall()
                versions = [dict(row) for row in rows]
                group = {
                        "fact_key": key["fact_key"],
                        "fact_label": key["fact_label"],
                        "versions": versions,
                        "current": next(
                            (row for row in versions if row.get("is_current")),
                            versions[-1] if versions else None,
                        ),
                    }
                if query:
                    corpus = " ".join(
                        [str(group["fact_label"])]
                        + [str(item.get("fact_value") or "") for item in versions]
                    )
                    direct = query.casefold() in corpus.casefold()
                    score = max(
                        fuzz.WRatio(query, str(group["fact_label"])),
                        fuzz.partial_ratio(query, corpus),
                    )
                    if not direct and score < 48:
                        continue
                    group["match_score"] = 100.0 if direct else round(float(score), 1)
                groups.append(group)
        if query:
            groups.sort(
                key=lambda item: (
                    float(item.get("match_score", 0)),
                    str((item.get("current") or {}).get("effective_date") or ""),
                ),
                reverse=True,
            )
        return groups[:safe_limit]

    async def list_facts(self, search: str = "", limit: int = 100) -> list[dict]:
        """List fact histories for the authenticated human manager."""
        if not self.enabled:
            return []
        return await asyncio.to_thread(self._list_facts_sync, search, limit)

    def count(self) -> int:
        if not self.enabled:
            return 0
        with self._connect() as connection:
            return int(
                connection.execute("SELECT COUNT(*) FROM fact_versions").fetchone()[0]
            )
