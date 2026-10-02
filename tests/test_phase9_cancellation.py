from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import AsyncMock

from agents.github_agent.agent import GitHubAgent
from rasool.eventbus import EventBus
from rasool.intel_feed import IntelFeedSink
from rasool.safety import Safety


class Phase9CancellationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)

        self.bus = EventBus()
        self.feed_path = self.root / "intel_feed.sqlite"
        self.sink = IntelFeedSink(
            self.bus,
            {"path": str(self.feed_path)},
        )
        await self.sink.start()

        self.agent = GitHubAgent(
            self.bus,
            config={"storage": {"cache_dir": str(self.root / "github_cache")}},
            safety=Safety({"intel.item", "intel.batch"}),
        )
        self.agent.cache_dir = self.root / "github_cache"
        self.agent.cache_dir.mkdir(parents=True, exist_ok=True)
        self.agent.queue_path = self.agent.cache_dir / "queue.sqlite"
        self.agent._initialize_queue()
        self.agent._seen = {}

        self.item = {
            "source": "github_trending",
            "title": "owner/thermal-safe-repo",
            "url": "https://github.com/owner/thermal-safe-repo",
            "summary": "A repository used to test cancellation durability.",
            "raw_lang": "en",
            "tags": ["Python", "testing"],
        }

    async def asyncTearDown(self) -> None:
        await self.sink.stop()
        self.temp_dir.cleanup()

    def _lifecycle_row(self) -> tuple[object, ...] | None:
        with sqlite3.connect(self.agent.queue_path) as connection:
            return connection.execute(
                """
                SELECT status, attempts, last_error, next_retry_at, metadata_json
                FROM item_lifecycle
                WHERE url = ?
                """,
                (self.item["url"],),
            ).fetchone()

    def _queue_row(self) -> tuple[object, ...] | None:
        with sqlite3.connect(self.agent.queue_path) as connection:
            return connection.execute(
                """
                SELECT url, attempts, next_retry_at, metadata_json
                FROM translation_queue
                WHERE url = ?
                """,
                (self.item["url"],),
            ).fetchone()

    def _feed_row(self) -> tuple[object, ...] | None:
        with sqlite3.connect(self.feed_path) as connection:
            return connection.execute(
                """
                SELECT url, title_ar, summary_ar, status
                FROM intel_feed
                WHERE url = ?
                """,
                (self.item["url"],),
            ).fetchone()

    async def test_cancelled_translation_is_queued_and_marked_retry_wait(self) -> None:
        translation_started = asyncio.Event()

        async def blocked_translation(record: dict[str, object]) -> dict[str, str]:
            translation_started.set()
            await asyncio.Event().wait()
            raise AssertionError("unreachable")

        self.agent._translate_record = blocked_translation  # type: ignore[method-assign]

        task = asyncio.create_task(self.agent._process_item(self.item))
        await asyncio.wait_for(translation_started.wait(), timeout=2)

        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task

        lifecycle = self._lifecycle_row()
        queue = self._queue_row()
        feed = self._feed_row()

        self.assertIsNotNone(lifecycle)
        self.assertIsNotNone(queue)
        self.assertIsNone(feed)

        assert lifecycle is not None
        assert queue is not None

        self.assertEqual(lifecycle[0], "retry_wait")
        self.assertEqual(lifecycle[1], 0)
        self.assertEqual(lifecycle[2], "cancelled:shutdown")
        self.assertIsInstance(lifecycle[3], float)
        self.assertGreater(float(lifecycle[3]), time.time() - 1)

        self.assertEqual(queue[0], self.item["url"])
        self.assertEqual(queue[1], 0)
        self.assertIsInstance(queue[2], float)

        self.assertNotIn(
            self.item["url"],
            self.agent._seen,
            "Cancelled item must not be marked seen before publish succeeds.",
        )

        metadata = json.loads(str(queue[3]))
        self.assertEqual(metadata["url"], self.item["url"])

    async def test_retry_wait_item_resumes_to_published_and_persisted(self) -> None:
        record = {
            "source": self.item["source"],
            "url": self.item["url"],
            "title": self.item["title"],
            "raw_title": self.item["title"],
            "raw_text": self.item["summary"],
            "src_lang": "en",
            "tags": self.item["tags"],
            "score": 0.0,
            "fetched_at": "2026-10-01T00:00:00Z",
        }

        await self.agent._queue_record(
            record,
            last_error="cancelled:shutdown",
            next_retry_at=time.time() - 1,
        )

        self.agent._translate_record = AsyncMock(
            return_value={
                "title_ar": "مستودع آمن حرارياً",
                "summary_ar": "وصف عربي لاختبار استئناف المعالجة.",
            }
        )

        self.agent._running = True
        try:
            await self.agent._retry_due_items()
        finally:
            self.agent._running = False

        lifecycle = self._lifecycle_row()
        queue = self._queue_row()
        feed = self._feed_row()

        self.assertIsNotNone(lifecycle)
        self.assertIsNone(queue)
        self.assertIsNotNone(feed)

        assert lifecycle is not None
        assert feed is not None

        self.assertEqual(lifecycle[0], "published")
        self.assertEqual(feed[0], self.item["url"])
        self.assertEqual(feed[1], "مستودع آمن حرارياً")
        self.assertEqual(feed[2], "وصف عربي لاختبار استئناف المعالجة.")
        self.assertEqual(feed[3], "persisted")

    async def test_cancelled_item_has_no_feed_row_before_resume(self) -> None:
        translation_started = asyncio.Event()

        async def blocked_translation(record: dict[str, object]) -> dict[str, str]:
            translation_started.set()
            await asyncio.Event().wait()
            raise AssertionError("unreachable")

        self.agent._translate_record = blocked_translation  # type: ignore[method-assign]

        task = asyncio.create_task(self.agent._process_item(self.item))
        await asyncio.wait_for(translation_started.wait(), timeout=2)

        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task

        self.assertIsNone(
            self._feed_row(),
            "A cancelled item must not become persisted before retry completes.",
        )

    async def test_cancelled_during_queue_flush_is_rescheduled(self) -> None:
        self.agent._translate_record = AsyncMock(return_value=None)
        queue_record = self.agent._queue_record
        calls = 0

        async def cancel_first_queue_flush(*args: object, **kwargs: object) -> None:
            nonlocal calls
            calls += 1
            if calls == 1:
                raise asyncio.CancelledError
            await queue_record(*args, **kwargs)

        self.agent._queue_record = AsyncMock(
            side_effect=cancel_first_queue_flush
        )

        with self.assertRaises(asyncio.CancelledError):
            await self.agent._process_item(self.item)

        queue = self._queue_row()
        lifecycle = self._lifecycle_row()
        self.assertIsNotNone(queue)
        self.assertIsNotNone(lifecycle)
        assert queue is not None
        assert lifecycle is not None
        self.assertEqual(queue[0], self.item["url"])
        self.assertEqual(lifecycle[0], "retry_wait")
        self.assertEqual(lifecycle[2], "cancelled:queue_flush")
        self.assertIsNone(self._feed_row())

        url_hash = hashlib.sha256(
            self.item["url"].encode("utf-8")
        ).hexdigest()
        self.assertNotIn(url_hash, self.agent._seen)
        self.assertEqual(self.agent._queue_record.await_count, 2)

    async def test_cancelled_during_publish_flush_is_rescheduled(self) -> None:
        self.agent._translate_record = AsyncMock(
            return_value={"title_ar": "عنوان", "summary_ar": "وصف"}
        )
        publish_started = asyncio.Event()
        never_release_publish = asyncio.Event()

        async def blocked_publish(*args: object, **kwargs: object) -> None:
            publish_started.set()
            await never_release_publish.wait()

        self.agent._publish_item = AsyncMock(side_effect=blocked_publish)

        task = asyncio.create_task(self.agent._process_item(self.item))
        await asyncio.wait_for(publish_started.wait(), timeout=5)
        task.cancel()

        with self.assertRaises(asyncio.CancelledError):
            await task

        queue = self._queue_row()
        lifecycle = self._lifecycle_row()
        self.assertIsNotNone(queue)
        self.assertIsNotNone(lifecycle)
        assert queue is not None
        assert lifecycle is not None
        self.assertEqual(queue[0], self.item["url"])
        self.assertEqual(lifecycle[0], "retry_wait")
        self.assertEqual(lifecycle[2], "cancelled:publish_flush")
        self.assertIsNone(self._feed_row())

        url_hash = hashlib.sha256(
            self.item["url"].encode("utf-8")
        ).hexdigest()
        self.assertNotIn(url_hash, self.agent._seen)

    async def test_three_cancellation_windows_are_all_protected(self) -> None:
        source = inspect.getsource(GitHubAgent._process_item)
        for cancellation_reason in (
            "cancelled:shutdown",
            "cancelled:queue_flush",
            "cancelled:publish_flush",
        ):
            with self.subTest(reason=cancellation_reason):
                self.assertIn(cancellation_reason, source)


if __name__ == "__main__":
    unittest.main()
