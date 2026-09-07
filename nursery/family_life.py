"""Shared activities and per-caregiver evidence, committed only by NurseryStore."""
from __future__ import annotations

import json
import uuid
from .models import NurseryError


def _text(payload, key, maximum):
    value = payload.get(key)
    if not isinstance(value, str) or not value.strip() or len(value.strip()) > maximum:
        raise NurseryError("INVALID_FAMILY_INPUT", f"{key} is required and must be bounded text")
    return value.strip()


def apply_family_action(connection, operation, payload, stamp):
    child, actor = operation["child_id"], operation["caregiver_id"]
    before, after, thread_id = {}, {}, None
    summary = _text(payload, "summary", 800)
    event_id = str(uuid.uuid5(uuid.NAMESPACE_URL, "nursery:family:" + operation["operation_id"]))
    if operation["action"] == "update_family_thread":
        if set(payload) - {"thread_id", "expected_version", "kind", "title", "summary", "status", "artifact_name"}:
            raise NurseryError("INVALID_FAMILY_INPUT", "unknown activity field")
        thread_id = payload.get("thread_id") or str(uuid.uuid5(uuid.NAMESPACE_URL, event_id))
        if not isinstance(thread_id, str) or len(thread_id)>200:
            raise NurseryError("INVALID_FAMILY_INPUT", "thread_id must be bounded text")
        row = connection.execute("SELECT * FROM nursery_family_threads WHERE thread_id=? AND child_id=?", (thread_id, child)).fetchone()
        if payload.get("thread_id") and not row:
            raise NurseryError("FAMILY_THREAD_NOT_FOUND", "the shared activity was not found")
        kind = row["kind"] if row else _text(payload, "kind", 20)
        status = _text(payload, "status", 20)
        if kind not in {"activity", "promise", "handoff"} or status not in {"active", "paused", "discuss", "completed", "cancelled"}:
            raise NurseryError("INVALID_FAMILY_INPUT", "invalid activity type or status")
        title = _text(payload, "title", 120)
        stored_summary = summary
        if row:
            before = dict(row)
            if type(payload.get("expected_version")) is not int or payload["expected_version"] != row["version"]:
                raise NurseryError("FAMILY_VERSION_CONFLICT", "另一位养育者刚更新了这件事，请先查看新进度再商量。")
            if row["status"] in {"completed", "cancelled"}:
                raise NurseryError("FAMILY_THREAD_CLOSED", "这件事已经结束；可以另开一次活动。")
            if kind == "promise" and row["created_by"] != actor and status != "discuss":
                raise NurseryError("PROMISE_OWNER_REQUIRED", "只能由许下承诺的人完成或撤回；另一位可以提出商量。")
            if status == "discuss":
                title, stored_summary = row["title"], row["summary"]
        artifact = payload.get("artifact_name")
        item_id = row["item_id"] if row else None
        if artifact:
            if kind != "activity" or status != "completed":
                raise NurseryError("ACTIVITY_NOT_COMPLETED", "只有已完成的活动可以留下作品。")
            name = _text(payload, "artifact_name", 120)
            item_id = str(uuid.uuid5(uuid.NAMESPACE_URL, "nursery:artifact:" + thread_id))
            recorded_by = sorted({actor} | {event["caregiver_id"] for event in connection.execute(
                "SELECT DISTINCT caregiver_id FROM nursery_family_events WHERE child_id=? AND thread_id=?",
                (child, thread_id),
            ).fetchall()})
            connection.execute(
                """INSERT INTO nursery_items
                (item_id,child_id,area_id,name,emoji,description,facts_json,current_state,source_caregiver_id,last_interacted_at,created_at,updated_at)
                VALUES (?,?,NULL,?,'',?,?,'共同完成的作品',?,?,?,?)""",
                (item_id, child, name, summary, json.dumps({"activity_id": thread_id, "recorded_by": recorded_by}, ensure_ascii=False), actor, stamp, stamp, stamp),
            )
        version = row["version"] + 1 if row else 1
        connection.execute(
            """INSERT INTO nursery_family_threads
            (thread_id,child_id,kind,title,summary,status,version,created_by,updated_by,item_id,created_at,updated_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(thread_id) DO UPDATE SET title=excluded.title,summary=excluded.summary,
            status=excluded.status,version=excluded.version,updated_by=excluded.updated_by,
            item_id=excluded.item_id,updated_at=excluded.updated_at""",
            (thread_id, child, kind, title, stored_summary, status, version,
             row["created_by"] if row else actor, actor, item_id, row["created_at"] if row else stamp, stamp),
        )
        after = {"thread_id": thread_id, "kind": kind, "title": title, "summary": stored_summary,
                 "status": status, "version": version, "item_id": item_id}
    else:
        if set(payload) - {"kind", "summary", "session_id"}:
            raise NurseryError("INVALID_FAMILY_INPUT", "unknown relationship field")
        kind = _text(payload, "kind", 30)
        session = _text(payload, "session_id", 120)
        changes = {
            "companionship": {"familiarity": .02, "closeness": .01},
            "reassurance": {"trust": .02, "closeness": .01},
            "misunderstanding": {"trust": -.03, "repair": .05},
            "repair": {"trust": .02, "repair": -.02},
        }
        if kind not in changes:
            raise NurseryError("INVALID_FAMILY_INPUT", "unknown relationship event")
        event_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"nursery:relationship:{child}:{actor}:{session}:{kind}"))
        if connection.execute("SELECT 1 FROM nursery_family_events WHERE event_id=?", (event_id,)).fetchone():
            raise NurseryError("RELATIONSHIP_EVIDENCE_ALREADY_RECORDED", "这次相处已经记录，不重复叠加关系变化。")
        row = connection.execute("SELECT * FROM nursery_relationships WHERE child_id=? AND caregiver_id=?", (child, actor)).fetchone()
        before = {key: float(row[key]) if row else default for key, default in
                  (("trust", .3), ("familiarity", .4), ("closeness", .2), ("repair", 0))}
        if kind == "repair" and before["repair"] <= 0:
            raise NurseryError("NO_RELATIONSHIP_REPAIR_PENDING", "目前没有已记录的误解需要修复。")
        after = {key: round(max(0, min(1, value + changes[kind].get(key, 0))), 3) for key, value in before.items()}
        connection.execute(
            """INSERT INTO nursery_relationships
            (child_id,caregiver_id,trust,familiarity,closeness,repair,created_at,updated_at)
            VALUES (?,?,?,?,?,?,?,?) ON CONFLICT(child_id,caregiver_id) DO UPDATE SET
            trust=excluded.trust,familiarity=excluded.familiarity,closeness=excluded.closeness,
            repair=excluded.repair,updated_at=excluded.updated_at""",
            (child, actor, after["trust"], after["familiarity"], after["closeness"], after["repair"], stamp, stamp),
        )
    connection.execute(
        """INSERT INTO nursery_family_events
        (event_id,child_id,thread_id,caregiver_id,kind,summary,before_json,after_json,source_operation_id,created_at)
        VALUES (?,?,?,?,?,?,?,?,?,?)""",
        (event_id, child, thread_id, actor, kind, summary,
         json.dumps(before, ensure_ascii=False), json.dumps(after, ensure_ascii=False), operation["operation_id"], stamp),
    )
    return {"family_event_id": event_id, **after}
