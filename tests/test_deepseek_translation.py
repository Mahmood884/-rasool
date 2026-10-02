import logging
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

import httpx

from agents.github_agent.agent import GitHubAgent
from rasool.eventbus import EventBus


class DeepSeekTranslationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.token_path = self.root / "deepseek_token"
        self.endpoint = "https://api.deepseek.com/chat/completions"
        self.model = "deepseek-flash"
        self.agent = GitHubAgent(
            EventBus(),
            config={
                "translation": {
                    "deepseek_enabled": True,
                    "deepseek_url": self.endpoint,
                    "deepseek_model": self.model,
                    "deepseek_token_file": str(self.token_path),
                    "deepseek_timeout_s": 17,
                    "deepseek_temperature": 0.15,
                    "deepseek_max_tokens": 128,
                    "deepseek_system_prompt": "Translate, preserve terms.",
                }
            },
        )

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    async def test_deepseek_uses_config_and_file_token_without_logging_it(
        self,
    ) -> None:
        api_key = "do-not-log-this-test-key"
        self.token_path.write_text(f"{api_key}\n", encoding="utf-8")
        response = Mock()
        response.raise_for_status = Mock()
        response.json.return_value = {
            "choices": [
                {"message": {"content": "وصف مترجم"}}
            ]
        }
        client = AsyncMock()
        client.post.return_value = response
        context_manager = AsyncMock()
        context_manager.__aenter__.return_value = client
        record: dict[str, str] = {}

        with self.assertNoLogs(
            "agents.github_agent.agent", level=logging.WARNING
        ):
            with patch("httpx.AsyncClient", return_value=context_manager) as factory:
                result = await self.agent._translate_deepseek(
                    "A technical description.", "en", record
                )

        self.assertEqual(result, "وصف مترجم")
        self.assertEqual(record["model_used"], "deepseek:deepseek-flash")
        factory.assert_called_once_with(timeout=17)
        client.post.assert_awaited_once()
        args, kwargs = client.post.await_args
        self.assertEqual(args[0], self.endpoint)
        self.assertEqual(kwargs["headers"], {"Authorization": f"Bearer {api_key}"})
        self.assertEqual(kwargs["json"]["model"], self.model)
        self.assertEqual(kwargs["json"]["temperature"], 0.15)
        self.assertEqual(kwargs["json"]["max_tokens"], 128)
        self.assertEqual(
            kwargs["json"]["messages"][0]["content"],
            "Translate, preserve terms.",
        )

    async def test_missing_token_skips_deepseek_and_uses_ollama(self) -> None:
        ollama = AsyncMock(return_value="ترجمة Ollama")
        self.agent._translate_ollama = ollama  # type: ignore[method-assign]

        with patch("httpx.AsyncClient") as client_factory:
            result = await self.agent._translate_local("Source text.", "en")

        self.assertEqual(result, "ترجمة Ollama")
        client_factory.assert_not_called()
        ollama.assert_awaited_once()

    async def test_deepseek_failure_falls_back_to_ollama_without_logging_key(
        self,
    ) -> None:
        api_key = "never-include-this-in-logs"
        self.token_path.write_text(api_key, encoding="utf-8")
        self.agent._translate_deepseek = AsyncMock(
            side_effect=httpx.ConnectError(f"failure mentioning {api_key}")
        )  # type: ignore[method-assign]
        ollama = AsyncMock(return_value="ترجمة Ollama")
        self.agent._translate_ollama = ollama  # type: ignore[method-assign]

        with self.assertLogs(
            "agents.github_agent.agent", level=logging.WARNING
        ) as captured:
            result = await self.agent._translate_local("Source text.", "en")

        self.assertEqual(result, "ترجمة Ollama")
        self.agent._translate_deepseek.assert_awaited_once()
        ollama.assert_awaited_once()
        log_output = "\n".join(captured.output)
        self.assertIn("falling back to Ollama", log_output)
        self.assertNotIn(api_key, log_output)


if __name__ == "__main__":
    unittest.main()
