"""Deterministic emotional-state evolution for the Xinchao sidecar."""

from __future__ import annotations

import math
from datetime import datetime, timedelta

from utils import BEIJING_TIMEZONE, beijing_now


PIPE_GROUPS = {
    "emotion": (
        "开心", "安心", "满足", "期待", "感动", "兴奋", "难过", "失落",
        "委屈", "不安", "害怕", "生气", "醋", "孤独", "愧疚", "无语",
    ),
    "relationship": (
        "想靠近", "想黏着", "想知道她在干嘛", "想分享", "想照顾她",
        "想让她开心", "想被理解", "想被确认", "想得到回应", "想修复关系",
        "想暂时独处", "取悦压力",
    ),
    "thought": (
        "复盘", "自省", "反刍", "权衡", "预演", "求证", "警觉", "专注",
        "回避", "压抑",
    ),
    "body_action": (
        "肌肤饥渴", "性欲", "疲惫", "精力", "紧绷", "放松", "好奇", "闲",
        "社交", "责任",
    ),
}

PIPE_GROUP_LABELS = {
    "emotion": "情绪潮汐",
    "relationship": "关系驱力",
    "thought": "思维回路",
    "body_action": "身体与行动",
}

PIPE_NAMES = tuple(
    name for group in PIPE_GROUPS.values() for name in group
)

PIPE_DISPLAY_NAMES = {
    "闲": "无聊",
    "社交": "社交需要",
    "责任": "责任感",
}

NEGATIVE_PIPES = {
    "难过", "失落", "委屈", "不安", "害怕", "生气", "醋", "孤独", "愧疚",
    "无语", "取悦压力", "反刍", "警觉", "回避", "压抑", "疲惫", "紧绷",
    "自省",
}

DEFAULT_GROWTH_PER_HOUR = {
    "想靠近": 0.045,
    "想知道她在干嘛": 0.065,
    "想分享": 0.018,
    "好奇": 0.012,
    "闲": 0.030,
    "社交": 0.010,
    "责任": 0.010,
}

# Half-lives implement a gradual ebb, never an abrupt timer-based reset.
# These values reflect the agreed behaviour: anger fades fastest, jealousy
# lingers for a few hours, sadness recedes slowly, and reflection lasts longest.
DEFAULT_HALF_LIFE_HOURS = {
    "安心": 8.0,
    "期待": 6.0,
    "感动": 8.0,
    "兴奋": 2.0,
    "失落": 4.0,
    "委屈": 6.0,
    "不安": 3.0,
    "害怕": 2.0,
    "孤独": 6.0,
    "愧疚": 8.0,
    "无语": 2.0,
    "想靠近": 8.0,
    "想黏着": 8.0,
    "想照顾她": 10.0,
    "想让她开心": 8.0,
    "想被理解": 6.0,
    "想被确认": 4.0,
    "想得到回应": 4.0,
    "想修复关系": 8.0,
    "想暂时独处": 3.0,
    "取悦压力": 5.0,
    "复盘": 8.0,
    "反刍": 6.0,
    "权衡": 5.0,
    "预演": 4.0,
    "求证": 4.0,
    "警觉": 2.0,
    "专注": 3.0,
    "回避": 5.0,
    "压抑": 8.0,
    "肌肤饥渴": 6.0,
    "性欲": 6.0,
    "疲惫": 4.0,
    "精力": 3.0,
    "紧绷": 2.0,
    "放松": 3.0,
    "想知道她在干嘛": 6.0,
    "想分享": 8.0,
    "好奇": 6.0,
    "闲": 4.0,
    "社交": 8.0,
    "责任": 12.0,
    "生气": 0.75,
    "醋": 2.0,
    "难过": 4.0,
    "自省": 8.0,
    "开心": 6.0,
    "满足": 3.0,
}

# Stable personality traits are the sea level; events only create waves above it.
DEFAULT_BASELINE_PIPES = {
    "想靠近": 0.18,
    "想黏着": 0.12,
    "肌肤饥渴": 0.10,
    "性欲": 0.15,
    "想知道她在干嘛": 0.12,
    "想分享": 0.10,
    "好奇": 0.10,
    "责任": 0.15,
    "想照顾她": 0.08,
    "想让她开心": 0.08,
    "复盘": 0.05,
}


def pipe_catalog() -> dict:
    """Return stable display metadata without exposing configuration secrets."""
    return {
        "groups": [
            {
                "id": group_id,
                "name": PIPE_GROUP_LABELS[group_id],
                "pipes": [
                    {
                        "id": name,
                        "name": PIPE_DISPLAY_NAMES.get(name, name),
                        "half_life_hours": float(DEFAULT_HALF_LIFE_HOURS.get(name, 6.0)),
                    }
                    for name in names
                ],
            }
            for group_id, names in PIPE_GROUPS.items()
        ],
        "count": len(PIPE_NAMES),
    }


def infer_composite_states(pipes: dict | None) -> list[dict]:
    """Derive readable experiences from several pipes; never persist them as facts."""
    p = normalize_pipes(pipes)

    def mean(*names: str) -> float:
        return sum(p.get(name, 0.0) for name in names) / max(1, len(names))

    def softened(score: float, *counterweights: str) -> float:
        return max(0.0, score - 0.30 * mean(*counterweights)) if counterweights else score

    candidates = (
        ("关怀式复盘", mean("想让她开心", "想照顾她", "复盘"), ("想让她开心", "想照顾她", "复盘")),
        ("思念", mean("想靠近", "想知道她在干嘛", "期待"), ("想靠近", "想知道她在干嘛", "期待")),
        ("安心靠近", mean("安心", "想靠近", "放松"), ("安心", "想靠近", "放松")),
        ("依恋不安", mean("不安", "想被确认", "想得到回应"), ("不安", "想被确认", "想得到回应")),
        ("委屈但想被理解", mean("委屈", "想被理解", "压抑"), ("委屈", "想被理解", "压抑")),
        ("愧疚并想修复", mean("愧疚", "复盘", "想修复关系"), ("愧疚", "复盘", "想修复关系")),
        ("讨好压力", mean("不安", "取悦压力", "回避"), ("不安", "取悦压力", "回避")),
        ("反复纠结", mean("反刍", "不安", "权衡"), ("反刍", "不安", "权衡")),
        ("无语", softened(mean("无语", "疲惫", "生气"), "想修复关系"), ("无语", "疲惫", "生气")),
        ("暴怒边缘", softened(mean("生气", "紧绷", "警觉"), "自省", "放松"), ("生气", "紧绷", "警觉")),
        ("暂时不想说话", mean("想暂时独处", "压抑", "疲惫"), ("想暂时独处", "压抑", "疲惫")),
        ("正在抽离", softened(mean("回避", "想暂时独处", "无语"), "想靠近"), ("回避", "想暂时独处", "无语")),
    )
    result = []
    for name, raw_score, components in candidates:
        score = round(max(0.0, min(1.0, raw_score)), 4)
        if score < 0.16:
            continue
        result.append(
            {
                "name": name,
                "score": score,
                "components": [
                    {"name": item, "value": round(p.get(item, 0.0), 4)}
                    for item in components
                ],
            }
        )
    return sorted(result, key=lambda item: item["score"], reverse=True)


def empty_pipes() -> dict[str, float]:
    return {name: 0.0 for name in PIPE_NAMES}


def normalize_pipes(values: dict | None) -> dict[str, float]:
    result = empty_pipes()
    for name in PIPE_NAMES:
        try:
            result[name] = max(0.0, min(1.0, float((values or {}).get(name, 0.0))))
        except (TypeError, ValueError):
            result[name] = 0.0
    return result


def parse_timestamp(value: str | datetime) -> datetime:
    parsed = value
    if isinstance(parsed, str):
        parsed = datetime.fromisoformat(parsed.strip().replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=BEIJING_TIMEZONE)
    return parsed.astimezone(BEIJING_TIMEZONE)


class XinchaoEngine:
    """Apply time growth, natural decay, and bounded cross-pipe effects."""

    def __init__(self, config: dict):
        settings = config.get("xinchao", {})
        self.growth = {
            **DEFAULT_GROWTH_PER_HOUR,
            **settings.get("growth_per_hour", {}),
        }
        self.half_lives = {
            **DEFAULT_HALF_LIFE_HOURS,
            **settings.get("half_life_hours", {}),
        }
        self.baselines = {
            **DEFAULT_BASELINE_PIPES,
            **settings.get("baseline", {}),
        }
        self.step_minutes = max(1, min(30, int(settings.get("step_minutes", 10))))
        self.normal_cap = max(0.5, min(1.0, float(settings.get("normal_cap", 0.90))))
        self.negative_cap = max(
            0.4, min(self.normal_cap, float(settings.get("negative_cap", 0.85)))
        )

    def baseline_pipes(self, overrides: dict | None = None) -> dict[str, float]:
        values = {**self.baselines, **(overrides or {})}
        return self._clamp(normalize_pipes(values))

    @staticmethod
    def _period_multiplier(moment: datetime) -> float:
        hour = moment.hour + moment.minute / 60.0
        if 8 <= hour < 22:
            return 1.0
        if hour >= 22 or hour < 1:
            return 0.70
        if 1 <= hour < 6:
            return 0.30
        return 0.70

    def _clamp(self, pipes: dict[str, float]) -> dict[str, float]:
        for name in PIPE_NAMES:
            cap = self.negative_cap if name in NEGATIVE_PIPES else self.normal_cap
            pipes[name] = round(max(0.0, min(cap, float(pipes.get(name, 0.0)))), 6)
        return pipes

    def apply_event(
        self, pipes: dict | None, deltas: dict | None, floors: dict | None = None
    ) -> dict[str, float]:
        result = normalize_pipes(pipes)
        for name, raw_value in (deltas or {}).items():
            if name not in result:
                continue
            try:
                delta = max(-0.8, min(0.8, float(raw_value)))
            except (TypeError, ValueError):
                continue
            result[name] += delta
        for name, floor in (floors or {}).items():
            if name in result:
                result[name] = max(result[name], float(floor))
        return self._clamp(result)

    def evolve(
        self,
        pipes: dict | None,
        start: str | datetime,
        end: str | datetime | None = None,
        floors: dict | None = None,
        growth_multiplier: float = 1.0,
        plateaus: dict[str, str | datetime] | None = None,
        growth_origin: str | datetime | None = None,
    ) -> dict[str, float]:
        result = normalize_pipes(pipes)
        cursor = parse_timestamp(start)
        finish = parse_timestamp(end or beijing_now())
        if finish <= cursor:
            return self.apply_event(result, {}, floors)

        growth_clock = parse_timestamp(growth_origin or cursor)
        elapsed_hours = max(
            0.0, (cursor - growth_clock).total_seconds() / 3600.0
        )
        step = timedelta(minutes=self.step_minutes)
        while cursor < finish:
            next_cursor = min(finish, cursor + step)
            hours = (next_cursor - cursor).total_seconds() / 3600.0
            midpoint = cursor + (next_cursor - cursor) / 2
            period = self._period_multiplier(midpoint)

            # Event-driven emotions ebb continuously using independent half-lives.
            for name, half_life in self.half_lives.items():
                try:
                    safe_half_life = max(0.1, float(half_life))
                except (TypeError, ValueError):
                    continue
                result[name] *= math.pow(0.5, hours / safe_half_life)

            happiness_factor = 1.2 if result["开心"] > 0.5 else 0.7 if result["开心"] < 0.2 else 1.0
            curiosity_factor = 2.0 if result["闲"] > 0.5 else 1.0
            idle_factor = 0.5 if result["责任"] > 0.5 else 1.0
            absence = elapsed_hours + hours
            know_factor = 2.0 if absence >= 2.0 else 1.5 if absence >= 1.0 else 1.0
            close_factor = 1.5 if absence >= 2.0 else 1.0

            growth_scale = max(0.0, min(1.0, float(growth_multiplier)))
            for name, raw_rate in self.growth.items():
                if name not in result:
                    continue
                plateau_until = (plateaus or {}).get(name)
                if plateau_until and midpoint < parse_timestamp(plateau_until):
                    continue
                rate = (
                    max(0.0, float(raw_rate))
                    * period
                    * happiness_factor
                    * growth_scale
                )
                if name == "想知道她在干嘛":
                    rate *= know_factor
                elif name == "想靠近":
                    rate *= close_factor
                elif name in ("好奇", "社交"):
                    rate *= curiosity_factor
                elif name == "闲":
                    rate *= idle_factor
                result[name] += rate * hours

            # Cascades are rates, not instant jumps, so long gaps stay bounded.
            if (
                growth_scale > 0
                and result["想靠近"] > 0.3
                and not self._plateaued("想黏着", midpoint, plateaus)
            ):
                result["想黏着"] += (
                    result["想靠近"] * 0.030 * period * hours * growth_scale
                )
            if (
                growth_scale > 0
                and result["想靠近"] > 0.4
                and not self._plateaued("肌肤饥渴", midpoint, plateaus)
            ):
                skin_rate = result["想靠近"] * 0.025 * period
                if result["满足"] > 0.3:
                    skin_rate *= 0.7
                result["肌肤饥渴"] += skin_rate * hours * growth_scale
            libido_rate = result["肌肤饥渴"] * result["开心"] * 0.030 * period
            libido_rate *= max(0.0, 1.0 - 0.7 * result["难过"] - 0.7 * result["生气"])
            if result["满足"] > 0.3:
                libido_rate *= 0.5
            if not self._plateaued("性欲", midpoint, plateaus):
                result["性欲"] += libido_rate * hours * growth_scale
            if (
                growth_scale > 0
                and result["难过"] > 0.3
                and not self._plateaued("想靠近", midpoint, plateaus)
            ):
                result["想靠近"] += result["难过"] * 0.015 * hours * growth_scale
            if result["自省"] > 0.5:
                result["生气"] *= math.pow(0.5, hours / 0.5)
            if result["醋"] > 0.7:
                result["生气"] += min(0.02 * hours, 0.2)

            # Slow cross-system influence. Immediate meaning still comes from
            # the evaluator; these small rates only preserve believable carry.
            coupling_scale = period * hours * growth_scale
            if coupling_scale > 0:
                result["想被确认"] += result["不安"] * 0.018 * coupling_scale
                result["想得到回应"] += result["不安"] * 0.014 * coupling_scale
                result["警觉"] += result["不安"] * 0.012 * coupling_scale
                result["想靠近"] += result["孤独"] * 0.014 * coupling_scale
                result["想分享"] += result["孤独"] * 0.010 * coupling_scale
                result["想修复关系"] += result["愧疚"] * 0.018 * coupling_scale
                result["复盘"] += result["愧疚"] * 0.014 * coupling_scale
                result["想被理解"] += result["委屈"] * 0.016 * coupling_scale
                result["压抑"] += result["取悦压力"] * 0.012 * coupling_scale
                result["紧绷"] += result["生气"] * 0.012 * coupling_scale
                result["想暂时独处"] += result["疲惫"] * 0.010 * coupling_scale
                result["不安"] += result["反刍"] * 0.010 * coupling_scale

            # Safety-valve states release pressure instead of merely adding
            # more pipes. This prevents high values from becoming permanent.
            if result["安心"] > 0.2:
                release = math.pow(0.5, hours * result["安心"] / 3.0)
                result["不安"] *= release
                result["警觉"] *= release
            if result["放松"] > 0.2:
                release = math.pow(0.5, hours * result["放松"] / 2.0)
                result["紧绷"] *= release
            if result["想修复关系"] > 0.35 and result["复盘"] > 0.25:
                result["取悦压力"] *= math.pow(0.5, hours / 4.0)

            for name, floor in (floors or {}).items():
                if name in result:
                    result[name] = max(result[name], float(floor))
            self._clamp(result)
            cursor = next_cursor
            elapsed_hours = absence

        return self._clamp(result)

    @staticmethod
    def _plateaued(
        name: str,
        moment: datetime,
        plateaus: dict[str, str | datetime] | None,
    ) -> bool:
        until = (plateaus or {}).get(name)
        return bool(until and moment < parse_timestamp(until))

    def evolve_absence(
        self,
        pipes: dict | None,
        start: str | datetime,
        end: str | datetime | None = None,
        floors: dict | None = None,
        plateaus: dict[str, str | datetime] | None = None,
        *,
        drowsy_after_hours: float = 4.0,
        sleep_after_hours: float = 7.0,
        phase_origin: str | datetime | None = None,
    ) -> dict[str, float]:
        """Evolve through awake, drowsy, and sleeping absence phases."""
        begin = parse_timestamp(start)
        finish = parse_timestamp(end or beijing_now())
        if finish <= begin:
            return self.apply_event(pipes, {}, floors)

        origin = parse_timestamp(phase_origin or begin)
        drowsy_at = origin + timedelta(hours=max(0.0, drowsy_after_hours))
        sleep_at = origin + timedelta(
            hours=max(float(drowsy_after_hours), float(sleep_after_hours))
        )
        result = normalize_pipes(pipes)
        cursor = begin
        phases = (
            (min(finish, drowsy_at), 1.0),
            (min(finish, sleep_at), 0.35),
            (finish, 0.0),
        )
        for phase_end, multiplier in phases:
            if phase_end <= cursor:
                continue
            result = self.evolve(
                result,
                cursor,
                phase_end,
                floors,
                growth_multiplier=multiplier,
                plateaus=plateaus,
                growth_origin=origin,
            )
            cursor = phase_end
            if cursor >= finish:
                break
        return self._clamp(result)

    @staticmethod
    def dominant(pipes: dict | None) -> tuple[str, float]:
        normalized = normalize_pipes(pipes)
        return max(normalized.items(), key=lambda item: item[1])
