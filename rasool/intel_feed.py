"""Persistent local sink for published intelligence items."""
import asyncio
import hashlib
import logging
import math
import sqlite3
import time
from pathlib import Path
from typing import Any

from rasool.eventbus import EventBus
from rasool.service import Service

logger = logging.getLogger(__name__)


class IntelFeedSink(Service):
    name = "intel_feed_sink"
    priority = "critical"

    def __init__(
        self,
        bus: EventBus,
        config: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(bus, config)
        settings = config or {}
        self.database_path = Path(
            str(
                settings.get(
                    "path", "~/.cache/rasool/intel_feed.sqlite"
                )
            )
        ).expanduser()
        self._subscribed = False

    async def start(self) -> None:
        if self._running:
            return
        await asyncio.to_thread(self._initialize_database)
        if not self._subscribed:
            self.bus.subscribe("intel.item", self._on_intel_item)
            self._subscribed = True
        self._running = True
        logger.info("started %s (database=%s)", self.name, self.database_path)

    async def stop(self) -> None:
        self._running = False

    async def _on_intel_item(self, topic: str, payload: Any) -> None:
        if not self._running:
            return
        if topic != "intel.item":
            raise ValueError(f"Unsupported Intel feed topic: {topic}")
        item = self._validate_payload(payload)
        await asyncio.to_thread(self._persist_item, item)

    @staticmethod
    def _validate_payload(payload: Any) -> dict[str, str | float]:
        if not isinstance(payload, dict):
            raise ValueError("intel.item payload must be an object")

        required_strings = (
            "source", "url", "title", "title_ar", "summary_ar", "fetched_at"
        )
        item: dict[str, str | float] = {}
        for field in required_strings:
            value = payload.get(field)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(
                    f"intel.item field {field!r} must be a non-empty string"
                )
            item[field] = value.strip()

        score = payload.get("score")
        if (
            isinstance(score, bool)
            or not isinstance(score, (int, float))
            or not math.isfinite(score)
        ):
            raise ValueError("intel.item field 'score' must be a finite number")
        item["score"] = float(score)
        return item

    def _initialize_database(self) -> None:
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(self.database_path, timeout=10) as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS intel_feed (
                    id TEXT PRIMARY KEY,
                    source TEXT NOT NULL,
                    url TEXT NOT NULL UNIQUE,
                    title_original TEXT NOT NULL,
                    title_ar TEXT NOT NULL,
                    summary_ar TEXT NOT NULL,
                    score REAL NOT NULL,
                    status TEXT NOT NULL CHECK (status = 'persisted'),
                    created_at TEXT NOT NULL,
                    persisted_at REAL NOT NULL
                )
                """
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS intel_feed_source_created "
                "ON intel_feed(source, created_at)"
            )

    def _persist_item(self, item: dict[str, str | float]) -> None:
        url = str(item["url"])
        item_id = hashlib.sha256(url.encode("utf-8")).hexdigest()
        with sqlite3.connect(self.database_path, timeout=10) as connection:
            connection.execute(
                """
                INSERT INTO intel_feed (
                    id, source, url, title_original, title_ar, summary_ar,
                    score, status, created_at, persisted_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, 'persisted', ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    source = excluded.source,
                    url = excluded.url,
                    title_original = excluded.title_original,
                    title_ar = excluded.title_ar,
                    summary_ar = excluded.summary_ar,
                    score = excluded.score,
                    status = excluded.status,
                    persisted_at = excluded.persisted_at
                """,
                (
                    item_id,
                    item["source"],
                    url,
                    item["title"],
                    item["title_ar"],
                    item["summary_ar"],
                    item["score"],
                    item["fetched_at"],
                    time.time(),
                ),
            )
        logger.info(
            "persisted intel item source=%s id=%s",
            item["source"],
            item_id[:12],
        )
