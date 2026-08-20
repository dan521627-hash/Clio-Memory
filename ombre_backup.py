#!/usr/bin/env python3
"""Create a consistent, append-only backup of Ombre Brain persistent data."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import re
import shutil
import sqlite3
from pathlib import Path
from typing import Iterable

from utils import beijing_now, now_iso


SNAPSHOT_NAME_RE = re.compile(r"^\d{8}-\d{6}$")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest().upper()


def _copy_stable(source: Path, destination: Path, attempts: int = 3) -> str:
    destination.parent.mkdir(parents=True, exist_ok=True)
    for _ in range(attempts):
        before = _sha256(source)
        shutil.copy2(source, destination)
        after = _sha256(source)
        copied = _sha256(destination)
        if before == after == copied:
            return copied
    raise RuntimeError(f"File changed repeatedly during backup: {source}")


def _backup_sqlite(source: Path, destination: Path) -> str:
    destination.parent.mkdir(parents=True, exist_ok=True)
    source_uri = f"{source.resolve().as_uri()}?mode=ro"
    source_db = sqlite3.connect(source_uri, uri=True, timeout=30)
    backup_db = sqlite3.connect(destination, timeout=30)
    try:
        source_db.backup(backup_db, pages=256, sleep=0.05)
    finally:
        backup_db.close()
        source_db.close()

    check_db = sqlite3.connect(destination)
    try:
        integrity = [row[0] for row in check_db.execute("PRAGMA integrity_check")]
    finally:
        check_db.close()
    if integrity != ["ok"]:
        raise RuntimeError(
            f"SQLite integrity check failed for {source.name}: {integrity}"
        )
    return _sha256(destination)


def _is_within(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
        return True
    except ValueError:
        return False


def create_backup(
    source_dir: str | Path,
    destination_dir: str | Path,
    *,
    snapshot_name: str | None = None,
    required_database_names: Iterable[str] = ("retrieval_feedback.sqlite3",),
) -> tuple[Path, dict]:
    source = Path(source_dir).resolve()
    destination_root = Path(destination_dir).resolve()
    if not source.is_dir():
        raise FileNotFoundError(f"Source directory does not exist: {source}")
    if _is_within(destination_root, source):
        raise ValueError("Backup destination must not be inside the source directory")

    name = snapshot_name or beijing_now().strftime("%Y%m%d-%H%M%S")
    if not SNAPSHOT_NAME_RE.fullmatch(name):
        raise ValueError("Snapshot name must use YYYYMMDD-HHMMSS")

    destination_root.mkdir(parents=True, exist_ok=True)
    final_dir = destination_root / name
    working_dir = destination_root / f".{name}.incomplete"
    if final_dir.exists() or working_dir.exists():
        raise FileExistsError(f"Backup snapshot already exists: {name}")
    working_dir.mkdir(parents=True)

    markdown_files = sorted(source.rglob("*.md"))
    private_config_files = sorted((source / "private").glob("*.json"))
    database_files = sorted(source.rglob("*.sqlite3"))
    database_names = {path.name for path in database_files}
    missing = sorted(set(required_database_names) - database_names)
    if missing:
        raise FileNotFoundError(
            "Required database missing from backup source: " + ", ".join(missing)
        )

    manifest: list[dict[str, str | int]] = []
    try:
        for source_path in markdown_files:
            relative = source_path.relative_to(source)
            target = working_dir / "memory" / relative
            digest = _copy_stable(source_path, target)
            manifest.append(
                {
                    "type": "memory",
                    "path": (Path("memory") / relative).as_posix(),
                    "bytes": target.stat().st_size,
                    "sha256": digest,
                }
            )

        for source_path in private_config_files:
            relative = source_path.relative_to(source)
            target = working_dir / "private" / source_path.name
            digest = _copy_stable(source_path, target)
            manifest.append(
                {
                    "type": "private_config",
                    "path": (Path("private") / source_path.name).as_posix(),
                    "bytes": target.stat().st_size,
                    "sha256": digest,
                }
            )

        for source_path in database_files:
            relative = source_path.relative_to(source)
            target = working_dir / "sqlite" / relative
            digest = _backup_sqlite(source_path, target)
            manifest.append(
                {
                    "type": "sqlite",
                    "path": (Path("sqlite") / relative).as_posix(),
                    "bytes": target.stat().st_size,
                    "sha256": digest,
                }
            )

        manifest_path = working_dir / "manifest-sha256.csv"
        with manifest_path.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(
                handle, fieldnames=("type", "path", "bytes", "sha256")
            )
            writer.writeheader()
            writer.writerows(manifest)

        report = {
            "status": "ok",
            "snapshot": name,
            "created_at": now_iso(),
            "markdown_files": len(markdown_files),
            "private_config_files": len(private_config_files),
            "sqlite_databases": len(database_files),
            "retrieval_feedback_included": (
                "retrieval_feedback.sqlite3" in database_names
            ),
            "integrity_checks": "ok",
            "automatic_deletion": False,
        }
        with (working_dir / "backup-report.json").open(
            "w", encoding="utf-8"
        ) as handle:
            json.dump(report, handle, ensure_ascii=False, indent=2)
            handle.write("\n")

        os.replace(working_dir, final_dir)
        return final_dir, report
    except Exception as error:
        try:
            with (working_dir / "backup-error.txt").open(
                "w", encoding="utf-8"
            ) as handle:
                handle.write(f"{type(error).__name__}: {error}\n")
        except OSError:
            pass
        raise


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", required=True)
    parser.add_argument("--destination", required=True)
    parser.add_argument("--snapshot-name")
    parser.add_argument(
        "--require",
        action="append",
        default=["retrieval_feedback.sqlite3"],
        dest="required_databases",
    )
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    final_dir, report = create_backup(
        args.source,
        args.destination,
        snapshot_name=args.snapshot_name,
        required_database_names=args.required_databases,
    )
    print(json.dumps({**report, "path": str(final_dir)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
