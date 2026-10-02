import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock

from agents.github_agent.agent import GitHubAgent
from rasool.eventbus import EventBus
from rasool.intel_feed import IntelFeedSink
from rasool.safety import Safety


class IntelFeedSinkTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.database_path = Path(self.temp_dir.name) / "intel_feed.sqlite"
        self.bus = EventBus()
        self.sink = IntelFeedSink(
            self.bus, {"path": str(self.database_path)}
        )
        await self.sink.start()
        self.payload = {
            "source": "github_trending",
            "title": "owner/repo",
            "url": "https://github.com/owner/repo",
            "title_ar": "مشروع",
            "summary_ar": "وصف المشروع",
            "score": 8.5,
            "fetched_at": "2026-10-01T08:00:00Z",
            "tags": ["Python"],
        }

    async def asyncTearDown(self) -> None:
        await self.sink.stop()
        self.temp_dir.cleanup()

    def _rows(self) -> list[tuple[object, ...]]:
        with sqlite3.connect(self.database_path) as connection:
            return list(
                connection.execute(
                    "SELECT source, url, title_original, title_ar, "
                    "summary_ar, score, status, created_at, persisted_at "
                    "FROM intel_feed"
                )
            )

    async def test_eventbus_item_is_persisted_with_translations(self) -> None:
        await self.bus.publish_sync("intel.item", self.payload)

        rows = self._rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][:8], (
            "github_trending",
            "https://github.com/owner/repo",
            "owner/repo",
            "مشروع",
            "وصف المشروع",
            8.5,
            "persisted",
            "2026-10-01T08:00:00Z",
        ))
        self.assertIsInstance(rows[0][8], float)

    async def test_repeated_url_updates_one_feed_row(self) -> None:
        await self.bus.publish_sync("intel.item", self.payload)
        updated = dict(self.payload, title_ar="عنوان محدث")
        await self.bus.publish_sync("intel.item", updated)

        rows = self._rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][3], "عنوان محدث")

    async def test_invalid_event_fails_without_persisting(self) -> None:
        invalid = dict(self.payload, score=float("nan"))

        with self.assertRaisesRegex(ValueError, "finite number"):
            await self.bus.publish_sync("intel.item", invalid)

        self.assertEqual(self._rows(), [])

    async def test_database_write_failure_fails_event_delivery(self) -> None:
        self.database_path.unlink()
        self.database_path.mkdir()

        with self.assertRaises(sqlite3.OperationalError):
            await self.bus.publish_sync("intel.item", self.payload)

    async def test_stop_prevents_later_event_persistence(self) -> None:
        await self.sink.stop()
        await self.bus.publish_sync("intel.item", self.payload)

        self.assertEqual(self._rows(), [])

    async def test_github_publisher_event_is_persisted_end_to_end(self) -> None:
        agent = GitHubAgent(
            self.bus,
            config={"storage": {"cache_dir": self.temp_dir.name}},
            safety=Safety({"intel.item", "intel.batch"}),
        )
        agent.queue_path = Path(self.temp_dir.name) / "github_queue.sqlite"
        agent._initialize_queue()
        agent._translate_record = AsyncMock(
            return_value={"title_ar": "مشروع", "summary_ar": "وصف المشروع"}
        )
        agent._seen = {}

        await agent._process_item({
            "source": "github_trending",
            "title": "owner/repo",
            "url": "https://github.com/owner/repo",
            "summary": "Project description",
            "raw_lang": "en",
            "tags": ["Python"],
        })

        rows = self._rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][0], "github_trending")
        self.assertEqual(rows[0][3], "مشروع")
        self.assertEqual(rows[0][6], "persisted")
        with sqlite3.connect(agent.queue_path) as connection:
            lifecycle_status = connection.execute(
                "SELECT status FROM item_lifecycle"
            ).fetchone()[0]
        self.assertEqual(lifecycle_status, "published")


if __name__ == "__main__":
    unittest.main()
