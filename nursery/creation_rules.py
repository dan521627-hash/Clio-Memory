"""Versioned, model-free rules for the stage-2 creation questionnaires."""

from __future__ import annotations

import hashlib
from typing import Any

from .models import NurseryError


TEMPERAMENT_QUESTIONNAIRE_VERSION = "temperament-v1"
INITIAL_STYLE_QUESTIONNAIRE_VERSION = "initial-style-v1"
TEMPERAMENT_FORMULA_VERSION = "temperament-blend-v1"

TEMPERAMENT_AXES = (
    "sensitivity",
    "activity",
    "closeness_need",
    "stranger_response",
    "emotional_intensity",
    "adaptation_speed",
    "persistence",
    "soothing_difficulty",
)


def _scale(low: float, middle: float, high: float) -> dict[str, float]:
    return {"low": low, "middle": middle, "high": high}


TEMPERAMENT_QUESTIONS: dict[str, dict[str, dict[str, float]]] = {
    "notice_and_strangers": {
        option: {"sensitivity": value, "stranger_response": value}
        for option, value in _scale(0.25, 0.50, 0.78).items()
    },
    "daily_activity": {
        option: {"activity": value}
        for option, value in _scale(0.25, 0.50, 0.78).items()
    },
    "closeness_preference": {
        option: {"closeness_need": value}
        for option, value in _scale(0.25, 0.50, 0.78).items()
    },
    "emotion_and_soothing": {
        option: {"emotional_intensity": value, "soothing_difficulty": value}
        for option, value in _scale(0.25, 0.50, 0.78).items()
    },
    "adapt_to_change": {
        option: {"adaptation_speed": value}
        for option, value in _scale(0.25, 0.50, 0.78).items()
    },
    "stay_with_failure": {
        option: {"persistence": value}
        for option, value in _scale(0.25, 0.50, 0.78).items()
    },
}

INITIAL_STYLE_QUESTIONS = (
    "respond_to_crying",
    "respect_refusal",
    "handle_mistakes",
    "encouragement",
    "conflict_repair",
    "create_safety",
)
STYLE_OPTIONS = _scale(0.25, 0.50, 0.75)

AXIS_LABELS = {
    "sensitivity": "对细节和气氛较敏锐",
    "activity": "更愿意主动活动和探索",
    "closeness_need": "更看重熟悉陪伴与亲近",
    "stranger_response": "面对陌生变化会先观察",
    "emotional_intensity": "情绪表达可能更鲜明",
    "adaptation_speed": "适应新变化相对更快",
    "persistence": "遇到困难时更愿意继续尝试",
    "soothing_difficulty": "需要更稳定而耐心的安抚",
}


def _validate_answer_map(
    answers: Any,
    *,
    question_ids: set[str],
    allowed_options: set[str],
) -> dict[str, str]:
    if not isinstance(answers, dict):
        raise NurseryError("INVALID_QUESTIONNAIRE", "answers must be an object")
    if set(answers) != question_ids:
        raise NurseryError(
            "QUESTIONNAIRE_INCOMPLETE",
            "all versioned questionnaire items must be answered exactly once",
        )
    clean: dict[str, str] = {}
    for question_id, option_id in answers.items():
        option = str(option_id).strip()
        if option not in allowed_options:
            raise NurseryError(
                "INVALID_QUESTIONNAIRE_OPTION",
                f"unsupported option for {question_id}",
            )
        clean[str(question_id)] = option
    return clean


def temperament_tendency(answers: Any) -> dict[str, float]:
    clean = _validate_answer_map(
        answers,
        question_ids=set(TEMPERAMENT_QUESTIONS),
        allowed_options={"low", "middle", "high"},
    )
    values: dict[str, list[float]] = {axis: [] for axis in TEMPERAMENT_AXES}
    for question_id, option_id in clean.items():
        for axis, value in TEMPERAMENT_QUESTIONS[question_id][option_id].items():
            values[axis].append(float(value))
    return {
        axis: round(sum(axis_values) / len(axis_values), 6)
        for axis, axis_values in values.items()
    }


def initial_style_profile(answers: Any) -> dict[str, float]:
    clean = _validate_answer_map(
        answers,
        question_ids=set(INITIAL_STYLE_QUESTIONS),
        allowed_options=set(STYLE_OPTIONS),
    )
    return {
        question_id: float(STYLE_OPTIONS[option_id])
        for question_id, option_id in clean.items()
    }


def independent_temperament(child_id: str) -> dict[str, float]:
    """Create one deterministic child-only vector; retries cannot reroll it."""

    identifier = str(child_id).strip()
    if not identifier:
        raise NurseryError("INVALID_CHILD_ID", "child_id is required")
    result: dict[str, float] = {}
    for axis in TEMPERAMENT_AXES:
        digest = hashlib.sha256(
            f"{TEMPERAMENT_FORMULA_VERSION}|{identifier}|{axis}".encode("utf-8")
        ).digest()
        unit = int.from_bytes(digest[:8], "big") / ((1 << 64) - 1)
        result[axis] = round(0.20 + unit * 0.60, 6)
    return result


def name_candidate_id(
    child_id: str,
    caregiver_id: str,
    ordinal: int,
    proposed_name: str,
) -> str:
    material = (
        f"name-candidate-v1|{str(child_id).strip()}|{str(caregiver_id).strip()}|"
        f"{int(ordinal)}|{str(proposed_name).strip()}"
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()[:24]


def blend_temperament(
    user_vector: dict[str, float],
    external_vector: dict[str, float],
    independent_vector: dict[str, float],
) -> dict[str, float]:
    vectors = (user_vector, external_vector, independent_vector)
    for vector in vectors:
        if set(vector) != set(TEMPERAMENT_AXES):
            raise NurseryError(
                "INVALID_TEMPERAMENT_VECTOR",
                "temperament vectors must contain the eight versioned axes",
            )
    return {
        axis: round(
            float(user_vector[axis]) * 0.30
            + float(external_vector[axis]) * 0.30
            + float(independent_vector[axis]) * 0.40,
            6,
        )
        for axis in TEMPERAMENT_AXES
    }


def temperament_summary(vector: dict[str, float]) -> str:
    if set(vector) != set(TEMPERAMENT_AXES):
        raise NurseryError(
            "INVALID_TEMPERAMENT_VECTOR",
            "temperament summary requires the eight versioned axes",
        )
    strongest = sorted(TEMPERAMENT_AXES, key=lambda axis: vector[axis], reverse=True)[:2]
    return "；".join(AXIS_LABELS[axis] for axis in strongest) + "。"
