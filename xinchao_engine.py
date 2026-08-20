"""Deterministic emotional-state evolution for the Xinchao sidecar."""

from __future__ import annotations

import math
from datetime import datetime, timedelta

from utils import BEIJING_TIMEZONE, beijing_now


PIPE_NAMES = (
    "想靠近",
    "想黏着",
    "肌肤饥渴",
    "性欲",
    "想知道她在干嘛",
    "想分享",
    "好奇",
    "闲",
    "社交",
    "责任",
    "难过",
    "生气",
    "醋",
    "自省",
    "开心",
    "满足",
)

NEGATIVE_PIPES = {"难过", "生气", "醋", "自省"}

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
}


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
    ) -> dict[str, float]:
        result = normalize_pipes(pipes)
        cursor = parse_timestamp(start)
        finish = parse_timestamp(end or beijing_now())
        if finish <= cursor:
            return self.apply_event(result, {}, floors)

        elapsed_hours = 0.0
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
    ) -> dict[str, float]:
        """Evolve through awake, drowsy, and sleeping absence phases."""
        begin = parse_timestamp(start)
        finish = parse_timestamp(end or beijing_now())
        if finish <= begin:
            return self.apply_event(pipes, {}, floors)

        drowsy_at = begin + timedelta(hours=max(0.0, drowsy_after_hours))
        sleep_at = begin + timedelta(
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
            )
            cursor = phase_end
            if cursor >= finish:
                break
        return self._clamp(result)

    @staticmethod
    def dominant(pipes: dict | None) -> tuple[str, float]:
        normalized = normalize_pipes(pipes)
        return max(normalized.items(), key=lambda item: item[1])
