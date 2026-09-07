"""Fail-closed loader for the one authoritative 113-rule YAML file."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml


EXPECTED_STAGE_IDS = ("infancy", "early_walker", "toddler", "young_child")
EXPECTED_DIMENSION_COUNT = 13
EXPECTED_ABILITY_COUNT = 113
STAGE_FIELDS = ("emerging_from", "assisted_from", "independent_from")


class CapabilityRulesError(ValueError):
    pass


@dataclass(frozen=True)
class CapabilityRules:
    schema_version: int
    stages: tuple[dict[str, Any], ...]
    abilities: tuple[dict[str, Any], ...]
    dimensions: frozenset[str]
    source_path: str


def ability_stage_ceiling(rules: CapabilityRules, stage_id: str, ability: dict) -> str:
    ranks = {stage["id"]: index for index, stage in enumerate(rules.stages)}
    if ability.get("hard_block", False) or stage_id not in ranks:
        return "blocked"
    for level in ("independent", "assisted", "emerging"):
        earliest = ability.get(level + "_from")
        if earliest in ranks and ranks[earliest] <= ranks[stage_id]:
            return level
    return "blocked"


def can_observe_ability(rules: CapabilityRules, stage_id: str, ability: dict) -> bool:
    return ability_stage_ceiling(rules, stage_id, ability) != "blocked"


def validate_growth_observation(payload: dict, rules: CapabilityRules | None, stage_id: str) -> None:
    """Phone and MCP use the same loaded catalog; an observation is not mastery."""
    from .models import NurseryError
    if rules is None:
        raise NurseryError("GROWTH_RULES_UNAVAILABLE", "成长规则暂未加载，没有保存观察。")
    observation = payload.get("observation")
    ids = observation.get("ability_ids") if isinstance(observation, dict) else None
    if (not isinstance(ids, list) or not 1 <= len(ids) <= 20
            or any(not isinstance(value, str) for value in ids) or len(set(ids)) != len(ids)):
        raise NurseryError("INVALID_GROWTH_OBSERVATION", "请选择不重复的成长观察项目。")
    available = {ability["id"] for ability in rules.abilities if can_observe_ability(rules, stage_id, ability)}
    if any(value not in available for value in ids):
        raise NurseryError("ABILITY_NOT_AVAILABLE", "观察项目不存在或当前阶段尚未开放，没有保存。")


def _require_bool(item: dict[str, Any], key: str, ability_id: str) -> None:
    if key in item and not isinstance(item[key], bool):
        raise CapabilityRulesError(f"{ability_id}.{key} must be boolean")


def _require_string_list(item: dict[str, Any], key: str, ability_id: str) -> None:
    if key not in item:
        return
    value = item[key]
    if not isinstance(value, list) or any(
        not isinstance(entry, str) or not entry.strip() for entry in value
    ):
        raise CapabilityRulesError(f"{ability_id}.{key} must be a string list")


def validate_rules_document(document: Any, *, source_path: str = "") -> CapabilityRules:
    if not isinstance(document, dict):
        raise CapabilityRulesError("rules root must be a mapping")
    schema_version = document.get("schema_version")
    if type(schema_version) is not int or schema_version != 1:
        raise CapabilityRulesError("schema_version must be the recognized value 1")

    stages = document.get("stages")
    if not isinstance(stages, list) or len(stages) != 4:
        raise CapabilityRulesError("rules must contain exactly four stages")
    stage_ids: list[str] = []
    for position, stage in enumerate(stages, start=1):
        if not isinstance(stage, dict):
            raise CapabilityRulesError("each stage must be a mapping")
        stage_id = stage.get("id")
        if stage_id != EXPECTED_STAGE_IDS[position - 1]:
            raise CapabilityRulesError("stage IDs or order are invalid")
        if stage.get("order") != position:
            raise CapabilityRulesError("stage order values must be 1 through 4")
        stage_ids.append(stage_id)
    stage_rank = {stage_id: index for index, stage_id in enumerate(stage_ids)}

    abilities = document.get("abilities")
    if not isinstance(abilities, list) or len(abilities) != EXPECTED_ABILITY_COUNT:
        raise CapabilityRulesError(
            f"rules must contain exactly {EXPECTED_ABILITY_COUNT} abilities"
        )

    seen_ids: set[str] = set()
    dimensions: set[str] = set()
    normalized: list[dict[str, Any]] = []
    for ability in abilities:
        if not isinstance(ability, dict):
            raise CapabilityRulesError("each ability must be a mapping")
        ability_id = ability.get("id")
        if not isinstance(ability_id, str) or not ability_id.strip():
            raise CapabilityRulesError("every ability requires a non-empty id")
        if ability_id in seen_ids:
            raise CapabilityRulesError(f"duplicate ability id: {ability_id}")
        seen_ids.add(ability_id)

        dimension = ability.get("dimension")
        if not isinstance(dimension, str) or not dimension.strip():
            raise CapabilityRulesError(f"{ability_id}.dimension must be a string")
        dimensions.add(dimension)

        evidence_sessions = ability.get("evidence_sessions")
        if type(evidence_sessions) is not int or evidence_sessions < 0:
            raise CapabilityRulesError(
                f"{ability_id}.evidence_sessions must be a non-negative integer"
            )
        _require_bool(ability, "hard_block", ability_id)
        _require_bool(ability, "guardian_confirmation", ability_id)
        _require_bool(ability, "culture_optional", ability_id)
        _require_string_list(ability, "safety_tags", ability_id)

        ranks: list[int] = []
        encountered_null = False
        for field_name in STAGE_FIELDS:
            value = ability.get(field_name)
            if value is None:
                encountered_null = True
                continue
            if value not in stage_rank:
                raise CapabilityRulesError(
                    f"{ability_id}.{field_name} references an unknown stage"
                )
            if encountered_null:
                raise CapabilityRulesError(
                    f"{ability_id} has a stage level after a null level"
                )
            ranks.append(stage_rank[value])
        if ranks != sorted(ranks):
            raise CapabilityRulesError(f"{ability_id} stage levels move backward")
        normalized.append(dict(ability))

    if len(dimensions) != EXPECTED_DIMENSION_COUNT:
        raise CapabilityRulesError(
            f"rules must contain exactly {EXPECTED_DIMENSION_COUNT} dimensions"
        )
    return CapabilityRules(
        schema_version=schema_version,
        stages=tuple(dict(item) for item in stages),
        abilities=tuple(normalized),
        dimensions=frozenset(dimensions),
        source_path=source_path,
    )


def load_capability_rules(path: str | Path) -> CapabilityRules:
    source = Path(path)
    try:
        text = source.read_text(encoding="utf-8")
    except OSError as exc:
        raise CapabilityRulesError(f"cannot read capability rules: {source}") from exc
    try:
        document = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise CapabilityRulesError(f"invalid YAML in capability rules: {source}") from exc
    return validate_rules_document(document, source_path=str(source.resolve()))


class CapabilityRulesGate:
    """Nursery-local availability gate; failure does not stop Anima itself."""

    def __init__(self, path: str | Path) -> None:
        self.path = str(path)
        self.rules: CapabilityRules | None = None
        self.error = ""

    def load(self) -> bool:
        try:
            self.rules = load_capability_rules(self.path)
            self.error = ""
            return True
        except CapabilityRulesError as exc:
            self.rules = None
            self.error = str(exc)
            return False

    def status(self) -> dict[str, Any]:
        return {
            "available": self.rules is not None,
            "schema_version": self.rules.schema_version if self.rules else None,
            "stage_count": len(self.rules.stages) if self.rules else 0,
            "dimension_count": len(self.rules.dimensions) if self.rules else 0,
            "ability_count": len(self.rules.abilities) if self.rules else 0,
            "error": self.error,
        }
