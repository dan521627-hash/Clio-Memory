# ============================================================
# Module: Memory Decay Engine (decay_engine.py)
# 模块：记忆衰减引擎
#
# Simulates human forgetting curve; auto-decays inactive memories and archives them.
# 模拟人类遗忘曲线，自动衰减不活跃记忆并归档。
#
# Core formula (improved Ebbinghaus + emotion coordinates):
# 核心公式（改进版艾宾浩斯遗忘曲线 + 情感坐标）：
#   Score = Importance × (activation_count^0.3) × e^(-λ×days) × emotion_weight
#
# Emotion weight (continuous coordinate, not discrete labels):
# 情感权重（基于连续坐标而非离散列举）：
#   emotion_weight = base + (arousal × arousal_boost)
#   Higher arousal → higher emotion weight → slower decay
#   唤醒度越高 → 情感权重越大 → 记忆衰减越慢
#
# Depended on by: server.py
# 被谁依赖：server.py
# ============================================================

import math
import asyncio
import logging
from datetime import datetime, timezone

logger = logging.getLogger("ombre_brain.decay")


class DecayEngine:
    """
    Memory decay engine — periodically scans dynamic buckets and reports
    low-activity candidates without moving or changing them.
    记忆衰减引擎 —— 定期扫描所有动态桶，
    计算衰减得分，仅报告低活跃候选，不移动或修改记忆桶。
    """

    def __init__(self, config: dict, bucket_mgr):
        # --- Load decay parameters / 加载衰减参数 ---
        decay_cfg = config.get("decay", {})
        self.decay_lambda = decay_cfg.get("lambda", 0.05)
        self.threshold = decay_cfg.get("threshold", 0.3)
        self.check_interval = decay_cfg.get("check_interval_hours", 24)
        configured_mode = str(decay_cfg.get("mode", "report_only")).strip().lower()
        if configured_mode != "report_only":
            logger.warning(
                "Unsafe decay mode %r ignored; decay is locked to report_only",
                configured_mode,
            )
        self.mode = "report_only"

        # --- Emotion weight params (continuous arousal coordinate) ---
        # --- 情感权重参数（基于连续 arousal 坐标）---
        emotion_cfg = decay_cfg.get("emotion_weights", {})
        self.emotion_base = emotion_cfg.get("base", 1.0)
        self.arousal_boost = emotion_cfg.get("arousal_boost", 0.8)

        self.bucket_mgr = bucket_mgr

        # --- Background task control / 后台任务控制 ---
        self._task: asyncio.Task | None = None
        self._running = False

    @property
    def is_running(self) -> bool:
        """Whether the decay engine is running in the background.
        衰减引擎是否正在后台运行。"""
        return self._running

    # ---------------------------------------------------------
    # Core: calculate decay score for a single bucket
    # 核心：计算单个桶的衰减得分
    #
    # Higher score = more vivid memory; below threshold → archive
    # 得分越高 = 记忆越鲜活，低于阈值则归档
    # Permanent buckets never decay / 固化桶永远不衰减
    # ---------------------------------------------------------
    # ---------------------------------------------------------
    # Time weight: 0-1d→1.0, day2→0.9, then ~10%/day, floor 0.3
    # 时间系数：0-1天=1.0，第2天=0.9，之后每天约降10%，7天后稳定在0.3
    # ---------------------------------------------------------
    @staticmethod
    def _calc_time_weight(days_since: float) -> float:
        """
        Piecewise time weight multiplier (multiplies base_score).
        分段式时间权重系数，作为 final_score 的乘数。
        """
        if days_since <= 1.0:
            return 1.0
        elif days_since <= 2.0:
            # Linear interpolation: 1.0→0.9 over [1,2]
            return 1.0 - 0.1 * (days_since - 1.0)
        else:
            # Exponential decay from 0.9, floor at 0.3
            # k = ln(3)/5 ≈ 0.2197 so that at day 7 (5 days past day 2) → 0.3
            raw = 0.9 * math.exp(-0.2197 * (days_since - 2.0))
            return max(0.3, raw)

    def calculate_score(self, metadata: dict) -> float:
        """
        Calculate current activity score for a memory bucket.
        计算一个记忆桶的当前活跃度得分。

        Formula: final_score = time_weight × base_score
          base_score = Importance × (act_count^0.3) × e^(-λ×days) × (base + arousal×boost)
          time_weight is the outer multiplier, takes priority over emotion factors.
        """
        if not isinstance(metadata, dict):
            return 0.0

        # --- Pinned/protected buckets: never decay, importance locked to 10 ---
        # --- 固化桶（pinned/protected）：永不衰减，importance 锁定为 10 ---
        if metadata.get("pinned") or metadata.get("protected"):
            return 999.0

        # --- Permanent buckets never decay / 固化桶永不衰减 ---
        if metadata.get("type") == "permanent":
            return 999.0

        importance = max(1, min(10, int(metadata.get("importance", 5))))
        activation_count = max(1, int(metadata.get("activation_count", 1)))

        # --- Days since last activation / 距离上次激活过了多少天 ---
        last_active_str = metadata.get("last_active", metadata.get("created", ""))
        try:
            last_active = datetime.fromisoformat(str(last_active_str))
            reference = (
                datetime.now()
                if last_active.tzinfo is None
                else datetime.now(timezone.utc).astimezone(last_active.tzinfo)
            )
            days_since = max(0.0, (reference - last_active).total_seconds() / 86400)
        except (ValueError, TypeError):
            days_since = 30  # Parse failure → assume 30 days / 解析失败假设已过 30 天

        # --- Emotion weight: continuous arousal coordinate ---
        # --- 情感权重：基于连续 arousal 坐标计算 ---
        # Higher arousal → stronger emotion → higher weight → slower decay
        # arousal 越高 → 情感越强烈 → 权重越大 → 衰减越慢
        try:
            arousal = max(0.0, min(1.0, float(metadata.get("arousal", 0.3))))
        except (ValueError, TypeError):
            arousal = 0.3
        emotion_weight = self.emotion_base + arousal * self.arousal_boost

        # --- Time weight (outer multiplier, highest priority) ---
        # --- 时间权重（外层乘数，优先级最高）---
        time_weight = self._calc_time_weight(days_since)

        # --- Base score = Importance × act_count^0.3 × e^(-λ×days) × emotion ---
        # --- 基础得分 ---
        base_score = (
            importance
            * (activation_count ** 0.3)
            * math.exp(-self.decay_lambda * days_since)
            * emotion_weight
        )

        # --- final_score = time_weight × base_score ---
        score = time_weight * base_score

        # --- Weight pool modifiers / 权重池修正因子 ---
        # Resolved events drop to 5%, sink to bottom awaiting keyword reactivation
        # 已解决的事件权重骤降到 5%，沉底等待关键词激活
        resolved_factor = 0.05 if metadata.get("resolved", False) else 1.0
        # High-arousal unresolved buckets get urgency boost for priority surfacing
        # 高唤醒未解决桶额外加成，优先浮现
        urgency_boost = 1.5 if (arousal > 0.7 and not metadata.get("resolved", False)) else 1.0

        return round(score * resolved_factor * urgency_boost, 4)

    # ---------------------------------------------------------
    # Execute one decay cycle
    # 执行一轮衰减周期
    # Scan all dynamic buckets → score → report those below threshold
    # 扫描所有动态桶 → 算分 → 报告低于阈值的候选
    # ---------------------------------------------------------
    async def run_decay_cycle(self) -> dict:
        """
        Execute one report-only decay cycle over dynamic buckets.
        执行一轮只读衰减演习，报告低于阈值的桶。

        Returns stats: {"checked": N, "archived": N, "lowest_score": X}
        """
        try:
            buckets = await self.bucket_mgr.list_all(include_archive=False)
        except Exception as e:
            logger.error(f"Failed to list buckets for decay / 衰减周期列桶失败: {e}")
            return {"checked": 0, "archived": 0, "lowest_score": 0, "error": str(e)}

        checked = 0
        archived = 0
        would_archive = []
        lowest_score = float("inf")

        for bucket in buckets:
            meta = bucket.get("metadata", {})

            # Skip permanent / pinned / protected buckets
            # 跳过固化桶和钉选/保护桶
            if meta.get("type") == "permanent" or meta.get("pinned") or meta.get("protected"):
                continue

            checked += 1
            try:
                score = self.calculate_score(meta)
            except Exception as e:
                logger.warning(
                    f"Score calculation failed for {bucket.get('id', '?')} / "
                    f"计算得分失败: {e}"
                )
                continue

            lowest_score = min(lowest_score, score)

            # This engine is permanently report-only. It may identify candidates,
            # but it never calls BucketManager.archive().
            if score < self.threshold:
                would_archive.append(
                    {
                        "bucket_id": bucket["id"],
                        "name": meta.get("name", bucket["id"]),
                        "score": score,
                    }
                )
                logger.info(
                    "Decay report-only candidate / 衰减演习候选: %s "
                    "(score=%.4f, threshold=%.4f)",
                    meta.get("name", bucket["id"]),
                    score,
                    self.threshold,
                )

        result = {
            "checked": checked,
            "archived": archived,
            "would_archive": would_archive,
            "mode": self.mode,
            "lowest_score": lowest_score if checked > 0 else 0,
        }
        logger.info(f"Decay cycle complete / 衰减周期完成: {result}")
        return result

    # ---------------------------------------------------------
    # Background decay task management
    # 后台衰减任务管理
    # ---------------------------------------------------------
    async def ensure_started(self) -> None:
        """
        Ensure the decay engine is started (lazy init on first call).
        确保衰减引擎已启动（懒加载，首次调用时启动）。
        """
        if not self._running:
            await self.start()

    async def start(self) -> None:
        """Start the background decay loop.
        启动后台衰减循环。"""
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self._background_loop())
        logger.info(
            f"Decay engine started, interval: {self.check_interval}h / "
            f"衰减引擎已启动，检查间隔: {self.check_interval} 小时"
        )

    async def stop(self) -> None:
        """Stop the background decay loop.
        停止后台衰减循环。"""
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        logger.info("Decay engine stopped / 衰减引擎已停止")

    async def _background_loop(self) -> None:
        """Background loop: run decay → sleep → repeat.
        后台循环体：执行衰减 → 睡眠 → 重复。"""
        while self._running:
            try:
                await self.run_decay_cycle()
            except Exception as e:
                logger.error(f"Decay cycle error / 衰减周期出错: {e}")
            # --- Wait for next cycle / 等待下一个周期 ---
            try:
                await asyncio.sleep(self.check_interval * 3600)
            except asyncio.CancelledError:
                break
