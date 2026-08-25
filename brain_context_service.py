"""Cross-layer read model for Clio's external-brain context."""

from __future__ import annotations

import asyncio
import math
from datetime import datetime

from mailbox_search import search_mailbox
from utils import beijing_now


AXIS_NAMES = {"X": "时间脉络", "Y": "关系牵引", "Z": "事实演化", "E": "情绪回响", "M": "记忆沉淀"}


def _clean(value, limit: int = 240) -> str:
    return " ".join(str(value or "").split())[:limit]


def _stamp(value: str) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value or "").strip().replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=beijing_now().tzinfo)
        return parsed.astimezone(beijing_now().tzinfo)
    except (TypeError, ValueError):
        return None


def _recency(value: str, half_life_days: float = 7.0) -> float:
    parsed = _stamp(value)
    if not parsed:
        return 0.12
    age_days = max(0.0, (beijing_now() - parsed).total_seconds() / 86400.0)
    return math.pow(0.5, age_days / max(0.25, half_life_days))


def _bucket_node(item: dict) -> dict:
    metadata = item.get("metadata") or {}
    matched = item.get("matched_segment") or {}
    return {
        "id": f"memory:{item.get('id') or metadata.get('id') or ''}", "source": "memory",
        "source_label": "记忆", "source_id": item.get("id") or metadata.get("id") or "",
        "title": metadata.get("name") or item.get("name") or "记忆",
        "text": _clean(matched.get("content") or item.get("content")),
        "time": metadata.get("updated") or metadata.get("created") or "", "dimensions": ["X", "Y", "M"],
        "importance": metadata.get("importance"), "topics": metadata.get("domain") or metadata.get("tags") or [],
        "reason": "保留了事情发生的时间、关系位置与可继续调用的内容",
    }


def _node_weight(node: dict) -> float:
    source_weight = {"trace": 1.0, "mailbox": 0.92, "thought": 0.88, "fact": 0.84, "memory": 0.72, "task": 0.62}.get(str(node.get("source") or ""), 0.55)
    return _recency(str(node.get("time") or "")) * source_weight


def _dimension_model(nodes: list[dict], state: dict, linkages: list[dict]) -> dict:
    counts = {axis: sum(1 for node in nodes if axis in node.get("dimensions", [])) for axis in AXIS_NAMES}
    activity = {axis: sum(_node_weight(node) for node in nodes if axis in node.get("dimensions", [])) for axis in AXIS_NAMES}
    pipes = {name: float(value or 0.0) for name, value in (state.get("pipes") or {}).items()}
    strongest_pipes = sorted(pipes.items(), key=lambda item: item[1], reverse=True)[:4]
    emotional_level = sum(value for _, value in strongest_pipes) / max(1, len(strongest_pipes))
    relation_names = {"想靠近", "想黏着", "想知道她在干嘛", "想分享", "想照顾她", "想让她开心", "想被理解", "想被确认", "想得到回应", "想修复关系"}
    relation_values = [pipes.get(name, 0.0) for name in relation_names if name in pipes]
    relation_level = sum(relation_values) / max(1, len(relation_values))
    latest = linkages[0] if linkages else {}
    latest_deltas = latest.get("pipe_deltas") or {}
    emotion_delta = max(-1.0, min(1.0, sum(float(v or 0) for v in latest_deltas.values())))
    relation_delta = max(-1.0, min(1.0, sum(float(latest_deltas.get(name, 0) or 0) for name in relation_names)))
    facts = [node for node in nodes if "Z" in node.get("dimensions", [])]
    recent_memory = [node for node in nodes if "M" in node.get("dimensions", [])]
    scores = {
        "X": min(0.96, 0.12 + 0.20 * math.log1p(activity["X"])),
        "Y": min(0.96, 0.08 + relation_level * 0.58 + 0.16 * math.log1p(activity["Y"])),
        "Z": min(0.96, 0.08 + 0.24 * math.log1p(activity["Z"] + sum(max(0, int(n.get("version_count") or 1) - 1) for n in facts))),
        "E": min(0.96, 0.08 + emotional_level * 0.62 + 0.14 * math.log1p(activity["E"])),
        "M": min(0.96, 0.10 + 0.24 * math.log1p(sum(_node_weight(n) for n in recent_memory))),
    }
    deltas = {
        "X": round(min(0.25, _recency(str((nodes[0] if nodes else {}).get("time") or "")) * 0.08), 3),
        "Y": round(relation_delta, 3),
        "Z": round(min(0.25, sum(1 for node in facts if _recency(str(node.get("time") or ""), 3) > 0.5) * 0.04), 3),
        "E": round(emotion_delta, 3),
        "M": round(min(0.25, sum(_node_weight(n) for n in recent_memory[:3]) * 0.05), 3),
    }
    latest_summary = _clean(latest.get("summary") or latest.get("event_summary"), 100)
    reasons = {
        "X": "由最近写入、事实日期和未竟进度共同确定",
        "Y": "由关系相关内容与关系驱力共同牵引",
        "Z": "由同一事实的新旧版本和确认状态共同形成",
        "E": "由当前激素值与最近一次真实写入的变化共同形成",
        "M": "由近期被写入、回想和继续使用的内容共同形成",
    }
    if latest_summary:
        reasons["E"] += f"；最近一次是“{latest_summary}”"
    return {axis: {"name": AXIS_NAMES[axis], "score": round(scores[axis], 3), "delta": deltas[axis], "evidence_count": counts[axis], "reason": reasons[axis], "drivers": [{"name": name, "value": round(value, 3)} for name, value in strongest_pipes[:3]] if axis == "E" else []} for axis in AXIS_NAMES}


async def build_brain_context(query: str, *, bucket_manager, mailbox_store, xinchao_service, task_service, fact_timeline_store, limit: int = 6) -> dict:
    """Read across source stores without copying or rewriting their bodies."""
    safe_limit = max(1, min(20, int(limit)))
    q = str(query or "").strip()
    if q:
        memory_job = bucket_manager.search(q, limit=safe_limit, use_semantic=True, include_sealed=False, record_feedback=False)
        mailbox_job = search_mailbox(mailbox_store, bucket_manager.embedding_index, q, limit=safe_limit)
        thought_job = xinchao_service.search_private_thoughts(q, kind="all", limit=safe_limit)
        task_job = task_service.search(q, limit=safe_limit, include_closed=True)
        fact_job = fact_timeline_store.list_facts(search=q, limit=safe_limit)
    else:
        memory_job = bucket_manager.list_all(include_archive=False, include_sealed=False)
        mailbox_job = mailbox_store.list(limit=safe_limit, include_deleted=False)
        thought_job = xinchao_service.list_private_thoughts(status="active", limit=safe_limit)
        task_job = task_service.store.list(limit=safe_limit)
        fact_job = fact_timeline_store.list_facts(limit=safe_limit)
    linkage_method = getattr(xinchao_service, "recent_linkages", None)
    linkage_job = linkage_method(limit=safe_limit) if linkage_method else asyncio.sleep(0, result=[])
    memory, mailbox, thoughts, tasks, facts, state, disposition, linkages = await asyncio.gather(
        memory_job, mailbox_job, thought_job, task_job, fact_job, xinchao_service.status(),
        xinchao_service.disposition_preview(30), linkage_job,
    )
    nodes = [_bucket_node(item) for item in list(memory)[:safe_limit]]
    nodes.extend({"id": f"mailbox:{item.get('message_id', '')}", "source": "mailbox", "source_label": "信箱", "source_id": item.get("message_id", ""), "title": "一封窗口交接信", "text": _clean(item.get("message")), "time": item.get("updated_at") or item.get("created_at") or "", "dimensions": ["X", "Y", "E", "M"], "reason": "把上一窗口的关系语境、当下感受和未完事项带入当前窗口"} for item in list(mailbox)[:safe_limit])
    nodes.extend({"id": f"thought:{item.get('canonical_tag', '')}", "source": "trace" if item.get("thought_kind") == "trace" else "thought", "source_label": item.get("kind_label") or ("念痕" if item.get("thought_kind") == "trace" else "心念"), "source_id": item.get("canonical_tag", ""), "title": item.get("event_tag") or item.get("kind_label") or "心念", "text": _clean(item.get("thought_text")), "time": item.get("last_seen") or item.get("first_seen") or "", "dimensions": ["X", "Y", "E", "M"], "effects": item.get("linkage") or {}, "reason": item.get("reason") or "这份想法在当前处境中浮现，并参与情绪与关系判断"} for item in list(thoughts)[:safe_limit])
    nodes.extend({"id": f"task:{item.get('task_id', '')}", "source": "task", "source_label": "未竟", "source_id": item.get("task_id", ""), "title": item.get("title") or "未竟", "text": _clean(item.get("details")), "time": item.get("updated_at") or item.get("created_at") or "", "dimensions": ["X", "M"], "importance": item.get("importance"), "status": item.get("status"), "reason": "保留仍需继续的行动与时间位置"} for item in list(tasks)[:safe_limit])
    for group in list(facts)[:safe_limit]:
        current = group.get("current") or {}
        nodes.append({"id": f"fact:{group.get('fact_key', '')}", "source": "fact", "source_label": "事实变化", "source_id": group.get("fact_key", ""), "title": group.get("fact_label") or "事实", "text": _clean(current.get("fact_value")), "time": current.get("effective_date") or current.get("recorded_at") or "", "dimensions": ["X", "Z", "M"], "version_count": len(group.get("versions") or []), "reason": f"同一事实已有 {len(group.get('versions') or [])} 个时间节点，当前显示最新版本"})
    nodes.sort(key=lambda item: _stamp(str(item.get("time") or "")) or datetime.min.replace(tzinfo=beijing_now().tzinfo), reverse=True)
    nodes = nodes[: safe_limit * 5]
    dimensions = _dimension_model(nodes, state, list(linkages))
    for node in nodes:
        node["dimension_labels"] = [AXIS_NAMES.get(axis, axis) for axis in node.get("dimensions", [])]
    return {"query": q, "nodes": nodes, "count": len(nodes), "dimensions": dimensions, "recent_linkages": list(linkages), "current_state": {"as_of": state.get("as_of"), "cycle_id": state.get("cycle_id"), "pipes": state.get("pipes") or {}, "timing": state.get("timing") or {}}, "disposition": disposition, "write_policy": "sources_remain_separate; derived effects are reversible"}
