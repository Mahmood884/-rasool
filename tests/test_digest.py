import sqlite3
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from rasool.digest import generate_digest, load_recent_items


class DigestTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.database_path = self.root / "intel_feed.sqlite"
        self.lifecycle_database_path = self.root / "queue.sqlite"
        self.output_dir = self.root / "digests"
        self.now = datetime(2026, 10, 1, 12, 0, tzinfo=timezone.utc)
        self._initialize_feed()
        self._initialize_lifecycle()

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def _initialize_feed(self) -> None:
        with sqlite3.connect(self.database_path) as connection:
            connection.execute(
                """
                CREATE TABLE intel_feed (
                    id TEXT PRIMARY KEY,
                    source TEXT NOT NULL,
                    url TEXT NOT NULL,
                    title_original TEXT NOT NULL,
                    title_ar TEXT NOT NULL,
                    summary_ar TEXT NOT NULL,
                    score REAL NOT NULL,
                    status TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    persisted_at REAL NOT NULL
                )
                """
            )
            now = self.now.timestamp()
            connection.executemany(
                """
                INSERT INTO intel_feed VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        "low",
                        "github_trending",
                        "https://github.com/org/low",
                        "org/low",
                        "org/low",
                        "ملخص أقل تقييماً",
                        2.0,
                        "persisted",
                        "2026-10-01T11:00:00Z",
                        now - 3600,
                    ),
                    (
                        "high",
                        "github_trending",
                        "https://github.com/org/high",
                        "org/high",
                        "org/high",
                        "ملخص أعلى تقييماً",
                        9.5,
                        "persisted",
                        "2026-10-01T11:30:00Z",
                        now - 1800,
                    ),
                    (
                        "old",
                        "github_trending",
                        "https://github.com/org/old",
                        "org/old",
                        "org/old",
                        "عنصر قديم",
                        100.0,
                        "persisted",
                        "2026-09-30T11:59:00Z",
                        now - 24 * 3600 - 60,
                    ),
                    (
                        "unpersisted",
                        "github_trending",
                        "https://github.com/org/unpersisted",
                        "org/unpersisted",
                        "org/unpersisted",
                        "لم يحفظ",
                        200.0,
                        "failed",
                        "2026-10-01T11:50:00Z",
                        now - 600,
                    ),
                ],
            )

    def _initialize_lifecycle(self) -> None:
        with sqlite3.connect(self.lifecycle_database_path) as connection:
            connection.execute(
                """
                CREATE TABLE item_lifecycle (
                    url TEXT NOT NULL,
                    metadata_json TEXT NOT NULL
                )
                """
            )
            connection.executemany(
                "INSERT INTO item_lifecycle VALUES (?, ?)",
                [
                    (
                        "https://github.com/org/high",
                        '{"raw_text": "Original high-priority source summary."}',
                    ),
                    (
                        "https://github.com/org/low",
                        '{"raw_text": "Original low-priority source summary."}',
                    ),
                ],
            )

    def test_recent_items_are_read_only_filtered_and_sorted_by_score(self) -> None:
        before = self.database_path.read_bytes()
        lifecycle_before = self.lifecycle_database_path.read_bytes()

        items = load_recent_items(
            self.database_path,
            since=self.now.timestamp() - 24 * 3600,
            lifecycle_database_path=self.lifecycle_database_path,
        )

        self.assertEqual([item.title_original for item in items], ["org/high", "org/low"])
        self.assertEqual(
            [item.summary_original for item in items],
            [
                "Original high-priority source summary.",
                "Original low-priority source summary.",
            ],
        )
        self.assertEqual(self.database_path.read_bytes(), before)
        self.assertEqual(
            self.lifecycle_database_path.read_bytes(), lifecycle_before
        )

    def test_digest_shows_unverified_translation_by_default(self) -> None:
        path, count = generate_digest(
            self.database_path,
            self.output_dir,
            now=self.now,
            lifecycle_database_path=self.lifecycle_database_path,
        )
        first_contents = path.read_text(encoding="utf-8")

        self.assertEqual(path, self.output_dir / "2026-10-01.md")
        self.assertEqual(count, 2)
        self.assertLess(
            first_contents.index("`org/high`"),
            first_contents.index("`org/low`"),
        )
        self.assertIn("**الوصف الأصلي:**", first_contents)
        self.assertIn("Original high-priority source summary.", first_contents)
        self.assertIn("https://github.com/org/high", first_contents)
        self.assertIn("محاولة ترجمة آلية — غير مدققة", first_contents)
        self.assertIn("ملخص أعلى تقييماً", first_contents)
        self.assertNotIn("org/old", first_contents)
        self.assertNotIn("org/unpersisted", first_contents)

    def test_unverified_translation_is_included_only_when_requested(self) -> None:
        path, count = generate_digest(
            self.database_path,
            self.output_dir,
            now=self.now,
            lifecycle_database_path=self.lifecycle_database_path,
            show_unverified_translation=True,
        )

        contents = path.read_text(encoding="utf-8")
        self.assertEqual(count, 2)
        self.assertIn(
            "> **محاولة ترجمة آلية — غير مدققة:**",
            contents,
        )
        self.assertIn("> ملخص أعلى تقييماً", contents)

    def test_unverified_translation_can_be_hidden_explicitly(self) -> None:
        path, _ = generate_digest(
            self.database_path,
            self.output_dir,
            now=self.now,
            lifecycle_database_path=self.lifecycle_database_path,
            show_unverified_translation=False,
        )

        contents = path.read_text(encoding="utf-8")
        self.assertNotIn("محاولة ترجمة آلية", contents)
        self.assertNotIn("ملخص أعلى تقييماً", contents)

    def test_empty_window_generates_explicit_empty_digest(self) -> None:
        path, count = generate_digest(
            self.database_path,
            self.output_dir,
            hours=0.01,
            now=self.now,
            lifecycle_database_path=self.lifecycle_database_path,
        )

        self.assertEqual(count, 0)
        self.assertIn(
            "No persisted items found in this time window.",
            path.read_text(encoding="utf-8"),
        )

    def test_invalid_window_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "finite positive"):
            generate_digest(
                self.database_path,
                self.output_dir,
                hours=0,
                now=self.now,
                lifecycle_database_path=self.lifecycle_database_path,
            )

    def test_missing_original_summary_is_reported_with_translation_by_default(
        self,
    ) -> None:
        path, _ = generate_digest(
            self.database_path,
            self.output_dir,
            now=self.now,
            lifecycle_database_path=self.root / "missing.sqlite",
        )

        content = path.read_text(encoding="utf-8")
        self.assertIn(
            "_النص الأصلي غير متاح في بيانات العنصر._",
            content,
        )
        self.assertIn("محاولة ترجمة آلية", content)
        self.assertIn("ملخص أعلى تقييماً", content)

    def test_missing_original_summary_can_still_show_translation_when_requested(
        self,
    ) -> None:
        path, _ = generate_digest(
            self.database_path,
            self.output_dir,
            now=self.now,
            lifecycle_database_path=self.root / "missing.sqlite",
            show_unverified_translation=True,
        )

        content = path.read_text(encoding="utf-8")
        self.assertIn(
            "_النص الأصلي غير متاح في بيانات العنصر._",
            content,
        )
        self.assertIn(
            "> **محاولة ترجمة آلية — غير مدققة:**",
            content,
        )
        self.assertIn("> ملخص أعلى تقييماً", content)


if __name__ == "__main__":
    unittest.main()
