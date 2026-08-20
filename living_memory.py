"""Unified LMC-5 coordinate snapshots stored outside Markdown buckets."""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
import os
import sqlite3
from pathlib import Path

from memory_segments import split_memory_segments
from utils import now_iso


class LivingMemoryStore:
    AXES = ("X", "Y", "Z", "E", "M")

    def __init__(self, config: dict):
        settings = config.get("living_memory", {})
        self.enabled = bool(settings.get("enabled", True))
        self.db_path = settings.get("db_path") or os.path.join(
            config["buckets_dir"], "living_memory.sqlite3"
        )
        if self.enabled:
            Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
            self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.db_path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA synchronous=NORMAL")
        return connection

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS living_memory_coordinates (
                    bucket_id TEXT PRIMARY KEY,
                    computed_at TEXT NOT NULL,
                    source_digest TEXT NOT NULL,
                    x_json TEXT NOT NULL,
                    y_json TEXT NOT NULL,
                    z_json TEXT NOT NULL,
                    e_json TEXT NOT NULL,
                    m_json TEXT NOT NULL,
                    completeness REAL NOT NULL DEFAULT 0,
                    provenance_json TEXT NOT NULL DEFAULT '{}'
                )
                """
            )

    @staticmethod
    def build(
        bucket: dict,
        *,
        relations: list[dict] | None = None,
        facts: list[dict] | None = None,
        topic: dict | None = None,
        decay_score: float | None = None,
    ) -> dict:
        metadata = bucket.get("metadata") or {}
        content = str(bucket.get("content") or "")
        segments = split_memory_segments(content, str(metadata.get("created") or ""))
        timestamps = [str(item.get("timestamp") or "") for item in segments if item.get("timestamp")]
        domains = metadata.get("domain") or []
        if isinstance(domains, str):
            domains = [domains]
        fact_versions = [item for item in (facts or []) if item]
        current_facts = [item for item in fact_versions if item.get("is_current")]
        relation_count = len(relations or [])
        segment_count = len(segments)

        def number(value, default=0.0):
            try:
                return float(value)
            except (TypeError, ValueError):
                return float(default)

        valence = max(-1.0, min(1.0, number(metadata.get("valence"))))
        arousal = max(0.0, min(1.0, number(metadata.get("arousal"))))
        tension = max(0.0, min(1.0, number(metadata.get("tension"))))
        importance = max(1.0, min(10.0, number(metadata.get("importance"), 5.0)))
        activation_count = max(0.0, number(metadata.get("activation_count")))

        # Each axis exposes a stable 0..1 score plus the evidence used to derive it.
        # Scores are coordinates for comparison, not claims about subjective experience.
        x_score = min(
            1.0,
            0.2
            + 0.16 * math.log2(max(1, segment_count) + 1)
            + (0.12 if metadata.get("trigger_date") else 0.0),
        )
        y_score = min(
            1.0,
            (1.0 - math.exp(-relation_count / 4.0)) * 0.75
            + (0.15 if topic else 0.0)
            + (0.10 if domains else 0.0),
        )
        z_score = min(
            1.0,
            (1.0 - math.exp(-len(fact_versions) / 3.0)) * 0.8
            + (0.2 if current_facts else 0.0),
        )
        e_score = min(1.0, max(abs(valence), arousal, tension))
        m_score = min(
            1.0,
            importance / 10.0 * 0.65
            + (1.0 - math.exp(-activation_count / 8.0)) * 0.2
            + (0.15 if metadata.get("pin_level") else 0.0),
        )
        coordinates = {
            "bucket_id": str(bucket.get("id") or ""),
            "X": {
                "score": round(x_score, 4),
                "created_at": str(metadata.get("created") or ""),
                "first_recorded_at": min(timestamps) if timestamps else str(metadata.get("created") or ""),
                "last_recorded_at": max(timestamps) if timestamps else str(metadata.get("created") or ""),
                "segment_count": len(segments),
                "trigger_date": str(metadata.get("trigger_date") or ""),
            },
            "Y": {
                "score": round(y_score, 4),
                "relation_count": relation_count,
                "domains": [str(item) for item in domains],
                "topic": topic or {},
                "related_buckets": [
                    {
                        "bucket_id": str(item.get("bucket_id") or item.get("target_id") or ""),
                        "score": item.get("similarity") or item.get("score"),
                    }
                    for item in (relations or [])[:12]
                ],
            },
            "Z": {
                "score": round(z_score, 4),
                "version_count": len(fact_versions),
                "current_facts": [
                    {
                        "fact": str(item.get("fact_label") or ""),
                        "value": str(item.get("fact_value") or ""),
                        "effective_date": str(item.get("effective_date") or ""),
                    }
                    for item in current_facts[:12]
                ],
            },
            "E": {
                "score": round(e_score, 4),
                "valence": round(valence, 4),
                "arousal": round(arousal, 4),
                "tension": round(tension, 4),
                "ai_feeling": str(metadata.get("ai_feeling") or ""),
                "feeling_memory": bool(metadata.get("feeling_memory", False)),
            },
            "M": {
                "score": round(m_score, 4),
                "importance": importance,
                "activation_count": int(activation_count),
                "pin_level": str(metadata.get("pin_level") or ""),
                "sealed": bool(metadata.get("sealed", False)),
                "archived": str(metadata.get("type") or "") == "archived",
                "resolved": bool(metadata.get("resolved", False)),
                "decay_score": decay_score,
            },
        }
        available = sum(
            bool(coordinates[axis])
            and any(value not in (None, "", [], {}) for value in coordinates[axis].values())
            for axis in LivingMemoryStore.AXES
        )
        coordinates["completeness"] = round(available / 5.0, 2)
        coordinates["provenance"] = {
            "X": "bucket segments and timestamps",
            "Y": "topic and relation sidecars",
            "Z": "fact timeline sidecar",
            "E": "bucket emotional metadata",
            "M": "bucket lifecycle metadata and decay score",
        }
        coordinates["coordinate"] = {
            axis: coordinates[axis]["score"] for axis in LivingMemoryStore.AXES
        }
        coordinates["source_digest"] = hashlib.sha256(
            (coordinates["bucket_id"] + "\0" + content + "\0" + json.dumps(metadata, ensure_ascii=False, sort_keys=True, default=str)).encode("utf-8")
        ).hexdigest()
        return coordinates

    def _save_sync(self, coordinates: dict) -> dict:
        stamp = now_iso()
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO living_memory_coordinates (
                    bucket_id, computed_at, source_digest,
                    x_json, y_json, z_json, e_json, m_json,
                    completeness, provenance_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(bucket_id) DO UPDATE SET
                    computed_at=excluded.computed_at,
                    source_digest=excluded.source_digest,
                    x_json=excluded.x_json,
                    y_json=excluded.y_json,
                    z_json=excluded.z_json,
                    e_json=excluded.e_json,
                    m_json=excluded.m_json,
                    completeness=excluded.completeness,
                    provenance_json=excluded.provenance_json
                """,
                (
                    coordinates["bucket_id"], stamp, coordinates["source_digest"],
                    json.dumps(coordinates["X"], ensure_ascii=False),
                    json.dumps(coordinates["Y"], ensure_ascii=False),
                    json.dumps(coordinates["Z"], ensure_ascii=False),
                    json.dumps(coordinates["E"], ensure_ascii=False),
                    json.dumps(coordinates["M"], ensure_ascii=False),
                    float(coordinates["completeness"]),
                    json.dumps(coordinates["provenance"], ensure_ascii=False),
                ),
            )
        result = dict(coordinates)
        result["computed_at"] = stamp
        return result

    async def save(self, coordinates: dict) -> dict:
        if not self.enabled:
            return {"status": "disabled"}
        return await asyncio.to_thread(self._save_sync, coordinates)

    def _get_sync(self, bucket_id: str) -> dict | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM living_memory_coordinates WHERE bucket_id=?",
                (str(bucket_id),),
            ).fetchone()
        if not row:
            return None
        item = dict(row)
        result = {
            "bucket_id": item["bucket_id"],
            "computed_at": item["computed_at"],
            "source_digest": item["source_digest"],
            "completeness": item["completeness"],
        }
        for axis in self.AXES:
            result[axis] = json.loads(item[f"{axis.lower()}_json"])
        result["coordinate"] = {
            axis: float((result[axis] or {}).get("score", 0.0))
            for axis in self.AXES
        }
        result["provenance"] = json.loads(item["provenance_json"])
        return result

    async def get(self, bucket_id: str) -> dict | None:
        if not self.enabled:
            return None
        return await asyncio.to_thread(self._get_sync, bucket_id)
