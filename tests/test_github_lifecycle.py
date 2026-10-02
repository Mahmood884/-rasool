import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from agents.github_agent.agent import GitHubAgent
from rasool.eventbus import EventBus
from rasool.safety import Safety


class GitHubLifecycleTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.agent = GitHubAgent(
            EventBus(),
            config={"storage": {"cache_dir": self.temp_dir.name}},
            safety=Safety({"intel.item", "intel.batch"}),
        )
        self.agent.cache_dir = Path(self.temp_dir.name)
        self.agent.queue_path = self.agent.cache_dir / "queue.sqlite"
        self.agent._initialize_queue()
        self.record = {
            "source": "github_trending",
            "url": "https://github.com/example/repo",
            "title": "example/repo",
            "raw_title": "example/repo",
            "raw_text": "description",
            "src_lang": "en",
            "tags": [],
            "score": 0.0,
            "fetched_at": "2026-10-01T00:00:00Z",
            "model_used": "qwen2.5:1.5b",
        }

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_item_lifecycle_persists_transitions_and_attempt_metadata(self) -> None:
        item_id = GitHubAgent._safe_error_summary("translation_retry", TimeoutError())
        self.assertEqual(item_id, "translation_retry:TimeoutError")

        self.agent._write_item_state(
            self.record, "fetched", None, None, None, None
        )
        self.agent._write_item_state(
            self.record, "retry_wait", 2, "translation_retry:TimeoutError",
            1_800_000_000.0, None,
        )
        self.agent._write_item_state(
            self.record, "published", 2, None, None, None
        )

        with sqlite3.connect(self.agent.queue_path) as connection:
            row = connection.execute(
                "SELECT status, attempts, last_error, next_retry_at, "
                "model_used, published_at FROM item_lifecycle"
            ).fetchone()

        self.assertEqual(row[0], "published")
        self.assertEqual(row[1], 2)
        self.assertIsNone(row[2])
        self.assertIsNone(row[3])
        self.assertEqual(row[4], "qwen2.5:1.5b")
        self.assertIsNotNone(row[5])

    def test_published_item_cannot_be_regressed(self) -> None:
        self.agent._write_item_state(
            self.record, "published", None, None, None, None
        )
        self.agent._write_item_state(
            self.record, "queued", 0, None, 1_800_000_000.0, None
        )

        counts = self.agent._load_lifecycle_counts()
        self.assertEqual(counts, {"published": 1})

    def test_github_headers_use_bearer_auth_without_real_token(self) -> None:
        with patch.object(
            GitHubAgent, "_load_github_token", return_value="unit-test-token"
        ):
            headers = self.agent._github_headers()

        self.assertEqual(headers["Authorization"], "Bearer unit-test-token")

    def test_github_headers_omit_auth_when_token_is_missing(self) -> None:
        with patch.object(GitHubAgent, "_load_github_token", return_value=None):
            headers = self.agent._github_headers()

        self.assertNotIn("Authorization", headers)


class GitHubItemProcessingTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.agent = GitHubAgent(
            EventBus(),
            config={"storage": {"cache_dir": self.temp_dir.name}},
            safety=Safety({"intel.item", "intel.batch"}),
        )
        self.agent.cache_dir = Path(self.temp_dir.name)
        self.agent.queue_path = self.agent.cache_dir / "queue.sqlite"
        self.agent._initialize_queue()
        self.agent._translate_record = AsyncMock(  # type: ignore[method-assign]
            return_value={"title_ar": "عنوان", "summary_ar": "وصف"}
        )
        self.item = {
            "source": "github_trending",
            "url": "https://github.com/example/repo",
            "title": "example/repo",
            "summary": "description",
            "raw_lang": "en",
            "tags": [],
        }

    async def asyncTearDown(self) -> None:
        self.temp_dir.cleanup()

    async def test_process_item_records_successful_publication(self) -> None:
        received: list[tuple[str, dict[str, object]]] = []

        async def consume(topic: str, payload: object) -> None:
            self.assertEqual(topic, "intel.item")
            self.assertIsInstance(payload, dict)
            received.append((topic, payload))

        self.agent.bus.subscribe("intel.item", consume)
        await self.agent._process_item(self.item)

        counts = self.agent._load_lifecycle_counts()
        self.assertEqual(counts, {"published": 1})
        self.assertEqual(len(received), 1)
        self.assertEqual(received[0][1]["url"], self.item["url"])
        self.assertEqual(received[0][1]["title_ar"], "عنوان")
        self.assertEqual(received[0][1]["summary_ar"], "وصف")

    async def test_process_item_records_queued_translation(self) -> None:
        self.agent._translate_record = AsyncMock(  # type: ignore[method-assign]
            return_value=None
        )

        await self.agent._process_item(self.item)

        counts = self.agent._load_lifecycle_counts()
        self.assertEqual(counts, {"queued": 1})

    async def test_denied_event_is_not_delivered_to_consumer(self) -> None:
        self.agent.safety = Safety()
        received: list[object] = []

        async def consume(topic: str, payload: object) -> None:
            received.append(payload)

        self.agent.bus.subscribe("intel.item", consume)
        with self.assertRaisesRegex(RuntimeError, "Safety gate"):
            await self.agent._publish("intel.item", {"url": self.item["url"]})

        self.assertEqual(received, [])

    async def test_system_idle_event_updates_translation_profile(self) -> None:
        await self.agent.bus.publish_sync("system.idle", {"idle": True})
        self.assertEqual(self.agent._translation_model, "qwen2.5:3b")
        self.assertEqual(self.agent.MAX_ITEMS_PER_PASS, 20)

        await self.agent.bus.publish_sync("system.idle", {"idle": False})
        self.assertEqual(self.agent._translation_model, "qwen2.5:1.5b")
        self.assertEqual(self.agent.MAX_ITEMS_PER_PASS, 5)

    async def test_title_is_not_translated(self) -> None:
        agent = GitHubAgent(
            EventBus(),
            config={"storage": {"cache_dir": self.temp_dir.name}},
        )
        agent._translate_local = AsyncMock(
            return_value="ملخص مترجم"
        )
        result = await agent._translate_record({
            "src_lang": "en",
            "raw_title": "owner/repo",
            "raw_text": "A long enough description to translate.",
        })

        self.assertIsNotNone(result)
        assert result is not None
        self.assertEqual(result["title_ar"], "owner/repo")
        self.assertEqual(result["summary_ar"], "ملخص مترجم")
        agent._translate_local.assert_awaited_once()
        self.assertEqual(
            agent._translate_local.await_args.args[0],
            "A long enough description to translate.",
        )
        self.assertNotIn(
            "owner/repo", agent._translate_local.await_args.args[0]
        )

    async def test_empty_summary_skips_translation(self) -> None:
        agent = GitHubAgent(
            EventBus(),
            config={"storage": {"cache_dir": self.temp_dir.name}},
        )
        agent._translate_local = AsyncMock()
        result = await agent._translate_record({
            "src_lang": "en",
            "raw_title": "owner/repo",
            "raw_text": "",
        })

        self.assertEqual(
            result, {"title_ar": "owner/repo", "summary_ar": ""}
        )
        agent._translate_local.assert_not_awaited()

    async def test_short_summary_skips_translation(self) -> None:
        agent = GitHubAgent(
            EventBus(),
            config={"storage": {"cache_dir": self.temp_dir.name}},
        )
        agent._translate_local = AsyncMock()
        result = await agent._translate_record({
            "src_lang": "en",
            "raw_title": "owner/repo",
            "raw_text": "short",
        })

        self.assertEqual(
            result, {"title_ar": "owner/repo", "summary_ar": ""}
        )
        agent._translate_local.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
