import unittest
from unittest.mock import AsyncMock

from agents.github_agent.agent import GitHubAgent
from rasool.eventbus import EventBus
from rasool.translation_guard import (
    is_repository_identifier,
    protect,
    restore,
    split_protected,
    split_repository_title,
)


class TranslationGuardTests(unittest.TestCase):
    def test_url_preserved(self) -> None:
        url = "https://github.com/owner/repo"
        masked, mapping = protect(f"Visit {url} now")

        self.assertIn("__GUARD_", masked)
        self.assertIn(url, mapping.values())

    def test_repo_preserved(self) -> None:
        masked, mapping = protect("The project owner/repo is great")

        self.assertIn("owner/repo", mapping.values())
        self.assertNotIn("The", mapping.values())
        self.assertNotIn("project", mapping.values())
        self.assertNotIn("owner/repo", masked)

    def test_camelcase_preserved(self) -> None:
        _, mapping = protect("Framework openMuse is built in TypeScript")

        self.assertIn("openMuse", mapping.values())
        self.assertIn("TypeScript", mapping.values())

    def test_roundtrip(self) -> None:
        original = "Use owner/repo with Python 3.12 at https://example.com"
        masked, mapping = protect(original)

        self.assertEqual(original, restore(masked, mapping))
        self.assertIn("__GUARD_", masked)
        self.assertNotIn("owner/repo", masked)
        self.assertNotIn("Python", masked)

    def test_no_false_positive_on_arabic(self) -> None:
        original = "نظام تشغيل"
        masked, mapping = protect(original)

        self.assertEqual(masked, original)
        self.assertFalse(mapping)

    def test_repository_title_classification(self) -> None:
        identifiers = (
            "mcncarl/jianying-headless",
            "shadcn-ui/lint",
            "CopilotKit/openmuse",
            "NVlabs/SoL-Pi",
        )
        for title in identifiers:
            with self.subTest(title=title):
                self.assertTrue(is_repository_identifier(title))
                self.assertIsNone(split_repository_title(title))

    def test_split_mixed_repository_title(self) -> None:
        self.assertEqual(
            split_repository_title(
                "Fast API client — mcncarl/jianying-headless"
            ),
            ("Fast API client", " — ", "mcncarl/jianying-headless"),
        )

    def test_structured_segments_keep_technical_tokens_separate(self) -> None:
        segments = split_protected(
            "Lightweight async web framework for Python using JSON"
        )
        protected = [text for text, is_protected in segments if is_protected]

        self.assertEqual(protected, ["Python", "JSON"])
        self.assertNotIn(("Python", False), segments)
        self.assertNotIn(("JSON", False), segments)


class TranslationGuardIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_repository_only_title_is_preserved_and_summary_translated(self) -> None:
        agent = GitHubAgent(EventBus(), config={})
        agent._translate_local = AsyncMock(return_value="ملخص مترجم")
        result = await agent._translate_record({
            "src_lang": "en",
            "raw_title": "owner/repo",
            "raw_text": "Long enough description to translate.",
        })

        self.assertEqual(
            result,
            {"title_ar": "owner/repo", "summary_ar": "ملخص مترجم"},
        )
        agent._translate_local.assert_awaited_once()
        self.assertEqual(
            agent._translate_local.await_args.args[0],
            "Long enough description to translate.",
        )

    async def test_mixed_title_is_preserved_verbatim(self) -> None:
        agent = GitHubAgent(EventBus(), config={})
        agent._translate_local = AsyncMock(return_value="ملخص مترجم")
        result = await agent._translate_record({
            "src_lang": "en",
            "raw_title": "Fast API client — owner/repo",
            "raw_text": "Description of the mixed-title repo.",
        })

        self.assertEqual(
            result,
            {
                "title_ar": "Fast API client — owner/repo",
                "summary_ar": "ملخص مترجم",
            },
        )
        agent._translate_local.assert_awaited_once()
        self.assertNotIn(
            "Fast API client — owner/repo",
            agent._translate_local.await_args.args,
        )

    async def test_descriptive_title_is_preserved_while_summary_is_translated(
        self,
    ) -> None:
        agent = GitHubAgent(EventBus(), config={})
        agent._translate_local = AsyncMock(return_value="ملخص مترجم")
        result = await agent._translate_record({
            "src_lang": "en",
            "raw_title": "A great Python library",
            "raw_text": "A long enough description to translate.",
        })

        self.assertEqual(
            result,
            {
                "title_ar": "A great Python library",
                "summary_ar": "ملخص مترجم",
            },
        )
        agent._translate_local.assert_awaited_once()
        self.assertEqual(
            agent._translate_local.await_args.args[0],
            "A long enough description to translate.",
        )
        self.assertNotIn(
            "A great Python library",
            agent._translate_local.await_args.args,
        )

    async def test_glossary_validation_is_metadata_only(self) -> None:
        agent = GitHubAgent(EventBus(), config={})
        agent._glossary = {
            "kernel": {
                "ar": "النواة",
                "filter_eligible": True,
            },
            "kernel panic": {
                "ar": "ذعر النواة",
                "filter_eligible": False,
            },
        }
        record = {
            "raw_title": "Kernel overview",
            "raw_text": "A kernel component.",
        }

        validation = agent._validate_translation(
            record, "نظرة عامة", "تعمل النواة بشكل صحيح."
        )

        self.assertEqual(
            validation,
            {
                "matched": [{
                    "term_en": "kernel",
                    "expected_ar": "النواة",
                    "filter_eligible": True,
                }],
                "unmatched_technical_terms": [],
            },
        )


if __name__ == "__main__":
    unittest.main()
