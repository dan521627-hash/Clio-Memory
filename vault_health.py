"""Read-only integrity checks for a Clio memory vault."""

from __future__ import annotations

import hashlib
import sqlite3
from collections import Counter
from contextlib import closing
from pathlib import Path

import frontmatter


class VaultHealthCheck:
    """Inspect Markdown buckets and SQLite sidecars without changing either."""

    def __init__(self, data_root: Path, embedding_index=None):
        self.data_root = Path(data_root).resolve()
        self.embedding_index = embedding_index

    def _memory_files(self) -> list[Path]:
        files = []
        for folder in ("permanent", "dynamic", "archive"):
            root = self.data_root / folder
            if root.exists():
                files.extend(root.rglob("*.md"))
        return sorted(files, key=lambda path: path.as_posix())

    def _check_markdown(self) -> tuple[dict, list[dict], set[str]]:
        files = self._memory_files()
        issues = []
        ids = []
        fingerprint = hashlib.sha256()
        total_bytes = 0
        for path in files:
            relative = path.relative_to(self.data_root).as_posix()
            try:
                raw = path.read_bytes()
                total_bytes += len(raw)
                digest = hashlib.sha256(raw).hexdigest()
                fingerprint.update(relative.encode("utf-8"))
                fingerprint.update(b"\0")
                fingerprint.update(digest.encode("ascii"))
                post = frontmatter.loads(raw.decode("utf-8"))
                bucket_id = str(post.get("id", "")).strip()
                if not bucket_id:
                    issues.append(
                        {"level": "error", "kind": "missing_id", "item": relative}
                    )
                else:
                    ids.append(bucket_id)
            except Exception as error:
                issues.append(
                    {
                        "level": "error",
                        "kind": "markdown_parse",
                        "item": relative,
                        "detail": str(error)[:180],
                    }
                )

        for bucket_id, count in Counter(ids).items():
            if count > 1:
                issues.append(
                    {
                        "level": "error",
                        "kind": "duplicate_id",
                        "item": bucket_id,
                        "detail": f"出现 {count} 次",
                    }
                )
        return (
            {
                "files": len(files),
                "bytes": total_bytes,
                "fingerprint_sha256": fingerprint.hexdigest(),
            },
            issues,
            set(ids),
        )

    def _check_sqlite(self) -> tuple[list[dict], list[dict]]:
        databases = []
        issues = []
        for path in sorted(self.data_root.glob("*.sqlite3")):
            relative = path.relative_to(self.data_root).as_posix()
            try:
                uri = f"file:{path.as_posix()}?mode=ro"
                with closing(sqlite3.connect(uri, uri=True, timeout=10)) as connection:
                    result = connection.execute("PRAGMA quick_check").fetchone()
                status = str(result[0] if result else "unknown")
                databases.append(
                    {"name": relative, "status": status, "bytes": path.stat().st_size}
                )
                if status.lower() != "ok":
                    issues.append(
                        {
                            "level": "error",
                            "kind": "sqlite_integrity",
                            "item": relative,
                            "detail": status[:180],
                        }
                    )
            except Exception as error:
                databases.append(
                    {"name": relative, "status": "unreadable", "bytes": path.stat().st_size}
                )
                issues.append(
                    {
                        "level": "error",
                        "kind": "sqlite_unreadable",
                        "item": relative,
                        "detail": str(error)[:180],
                    }
                )
        return databases, issues

    def run(self) -> dict:
        memory, memory_issues, bucket_ids = self._check_markdown()
        databases, database_issues = self._check_sqlite()
        issues = memory_issues + database_issues
        pending_embeddings = 0
        indexed_embeddings = 0
        if self.embedding_index is not None and self.embedding_index.enabled:
            try:
                pending_embeddings = int(self.embedding_index.pending_count())
                indexed_embeddings = int(self.embedding_index.count())
                if pending_embeddings:
                    issues.append(
                        {
                            "level": "warning",
                            "kind": "embedding_queue",
                            "item": "embedding_queue",
                            "detail": f"还有 {pending_embeddings} 条向量等待生成",
                        }
                    )
            except Exception as error:
                issues.append(
                    {
                        "level": "warning",
                        "kind": "embedding_status",
                        "item": "embeddings.sqlite3",
                        "detail": str(error)[:180],
                    }
                )
        level = "error" if any(item["level"] == "error" for item in issues) else (
            "warning" if issues else "ok"
        )
        return {
            "status": level,
            "memory": memory,
            "database_count": len(databases),
            "databases": databases,
            "bucket_id_count": len(bucket_ids),
            "embedding_count": indexed_embeddings,
            "embedding_queue": pending_embeddings,
            "issues": issues,
        }
