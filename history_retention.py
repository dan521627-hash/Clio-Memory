import asyncio
import logging


logger = logging.getLogger("ombre_brain.history_retention")


class HistoryRetentionEngine:
    def __init__(self, config: dict, bucket_mgr, history_store):
        settings = config.get("history", {}).get("retention", {})
        self.bucket_mgr = bucket_mgr
        self.history_store = history_store
        self.enabled = bool(settings.get("enabled", True))
        configured_mode = str(settings.get("mode", "report_only")).strip().lower()
        if configured_mode != "report_only":
            logger.warning(
                "Unsafe history retention mode %r ignored; stage one is locked to report_only",
                configured_mode,
            )
        self.mode = "report_only"
        self.retention_days = max(1, int(settings.get("retention_days", 10)))
        self.min_versions_per_bucket = max(
            1, int(settings.get("min_versions_per_bucket", 3))
        )
        self.protect_delete_snapshots = bool(
            settings.get("protect_delete_snapshots", True)
        )
        self.protect_important_snapshots = bool(
            settings.get("protect_important_snapshots", True)
        )
        self.check_interval_hours = max(
            1, int(settings.get("check_interval_hours", 24))
        )
        self._running = False
        self._task = None

    async def preview(self) -> dict:
        protected_bucket_ids = set()
        if self.protect_important_snapshots:
            buckets = await self.bucket_mgr.list_all(
                include_archive=True, include_sealed=True
            )
            for bucket in buckets:
                metadata = bucket.get("metadata", {})
                if any(
                    metadata.get(key, False)
                    for key in ("pinned", "protected", "sealed")
                ):
                    protected_bucket_ids.add(bucket["id"])
        report = await self.history_store.preview_retention(
            retention_days=self.retention_days,
            min_versions_per_bucket=self.min_versions_per_bucket,
            protected_bucket_ids=protected_bucket_ids,
            protect_delete_snapshots=self.protect_delete_snapshots,
            protect_important_snapshots=self.protect_important_snapshots,
        )
        report["mode"] = self.mode
        return report

    async def run_cycle(self) -> dict:
        if not self.enabled:
            return {
                "mode": self.mode,
                "enabled": False,
                "checked": 0,
                "candidate_count": 0,
                "candidates": [],
            }
        report = await self.preview()
        logger.info(
            "History retention preview: checked=%s candidates=%s days=%s keep=%s mode=%s",
            report["checked"],
            report["candidate_count"],
            self.retention_days,
            self.min_versions_per_bucket,
            self.mode,
        )
        for candidate in report["candidates"]:
            logger.info(
                "History retention candidate: snapshot=%s bucket=%s at=%s operation=%s",
                candidate["snapshot_id"],
                candidate["bucket_id"],
                candidate["snapshot_at"],
                candidate["operation_type"],
            )
        return report

    async def ensure_started(self) -> None:
        if self.enabled and not self._running:
            await self.start()

    async def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self._background_loop())
        logger.info(
            "History retention preview started: interval=%sh days=%s keep=%s mode=%s",
            self.check_interval_hours,
            self.retention_days,
            self.min_versions_per_bucket,
            self.mode,
        )

    async def stop(self) -> None:
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        logger.info("History retention preview stopped")

    async def _background_loop(self) -> None:
        while self._running:
            try:
                await self.run_cycle()
            except Exception as error:
                logger.error("History retention preview failed: %s", error)
            try:
                await asyncio.sleep(self.check_interval_hours * 3600)
            except asyncio.CancelledError:
                break
