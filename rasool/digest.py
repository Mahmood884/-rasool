"""Generate a local Markdown digest from the persisted intelligence feed."""
import argparse
import json
import math
import os
import sqlite3
import sys
import tempfile
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote

DEFAULT_DATABASE = Path.home() / ".cache/rasool/intel_feed.sqlite"
DEFAULT_LIFECYCLE_DATABASE = (
    Path.home() / ".cache/rasool/github_agent/queue.sqlite"
)
DEFAULT_OUTPUT_DIR = Path.home() / ".cache/rasool/digests"


@dataclass(frozen=True)
class FeedItem:
    source: str
    url: str
    title_original: str
    summary_original: str | None
    summary_ar: str
    score: float


def _read_original_summaries(
    database_path: Path,
    urls: list[str],
) -> dict[str, str]:
    if not urls or not database_path.expanduser().is_file():
        return {}

    database_uri = f"{database_path.expanduser().resolve().as_uri()}?mode=ro"
    summaries: dict[str, str] = {}
    with sqlite3.connect(database_uri, uri=True) as connection:
        for offset in range(0, len(urls), 900):
            batch = urls[offset:offset + 900]
            placeholders = ",".join("?" for _ in batch)
            rows = connection.execute(
                "SELECT url, metadata_json FROM item_lifecycle "
                f"WHERE url IN ({placeholders})",
                batch,
            ).fetchall()
            for url, metadata_json in rows:
                try:
                    metadata = json.loads(metadata_json)
                except (TypeError, json.JSONDecodeError) as exc:
                    raise ValueError(
                        f"Invalid lifecycle metadata for {url}: {exc}"
                    ) from exc
                if not isinstance(metadata, dict):
                    raise ValueError(
                        f"Lifecycle metadata for {url} must be a JSON object"
                    )
                raw_summary = metadata.get("raw_text")
                if isinstance(raw_summary, str):
                    summaries[url] = raw_summary.strip()
    return summaries


def load_recent_items(
    database_path: Path,
    *,
    since: float,
    lifecycle_database_path: Path = DEFAULT_LIFECYCLE_DATABASE,
) -> list[FeedItem]:
    """Read persisted feed rows without opening the database for writes."""
    database_uri = f"{database_path.expanduser().resolve().as_uri()}?mode=ro"
    with sqlite3.connect(database_uri, uri=True) as connection:
        rows = connection.execute(
            """
            SELECT source, url, title_original, summary_ar, score
            FROM intel_feed
            WHERE status = 'persisted' AND persisted_at >= ?
            ORDER BY score DESC, persisted_at DESC, id ASC
            """,
            (since,),
        ).fetchall()
    original_summaries = _read_original_summaries(
        lifecycle_database_path,
        [row[1] for row in rows],
    )
    return [
        FeedItem(
            source=source,
            url=url,
            title_original=title_original,
            summary_original=original_summaries.get(url),
            summary_ar=summary_ar,
            score=score,
        )
        for source, url, title_original, summary_ar, score in rows
    ]


def _markdown_title(title: str) -> str:
    escaped = title.replace("\\", "\\\\").replace("`", "\\`")
    return f"`{escaped}`"


def _markdown_url(url: str) -> str:
    return quote(url, safe="/:?=&%#@+,-._~")


def render_digest(
    items: list[FeedItem],
    *,
    generated_at: datetime,
    since: datetime,
    hours: float,
    show_unverified_translation: bool = True,
) -> str:
    generated_local = generated_at.astimezone()
    lines = [
        f"# Rasool Digest — {generated_local:%Y-%m-%d}",
        "",
        f"- Generated: {generated_local.isoformat(timespec='seconds')}",
        f"- Window: last {hours:g} hours (since {since.astimezone().isoformat(timespec='seconds')})",
        f"- Items: {len(items)}",
        "",
    ]
    if not items:
        lines.extend(["No persisted items found in this time window.", ""])
        return "\n".join(lines)

    for item in items:
        lines.extend([
            "---",
            "",
            f"## {_markdown_title(item.title_original)}",
            "",
            f"- Source: {item.source}",
            f"- Score: {item.score:g}",
            f"- Link: [{item.url}]({_markdown_url(item.url)})",
            "",
            "**الوصف الأصلي:**",
            "",
            (
                item.summary_original
                if item.summary_original
                else "_النص الأصلي غير متاح في بيانات العنصر._"
            ),
            "",
        ])
        if show_unverified_translation:
            lines.extend([
                "> **محاولة ترجمة آلية — غير مدققة:**",
                ">",
                *(
                    f"> {line}"
                    for line in (
                        item.summary_ar.splitlines()
                        if item.summary_ar
                        else ["_لا توجد محاولة ترجمة متاحة._"]
                    )
                ),
                "",
            ])
    return "\n".join(lines)


def generate_digest(
    database_path: Path = DEFAULT_DATABASE,
    output_dir: Path = DEFAULT_OUTPUT_DIR,
    *,
    hours: float = 24,
    now: datetime | None = None,
    lifecycle_database_path: Path = DEFAULT_LIFECYCLE_DATABASE,
    show_unverified_translation: bool = True,
) -> tuple[Path, int]:
    """Write the latest digest atomically and return its path and item count."""
    if not math.isfinite(hours) or hours <= 0:
        raise ValueError("hours must be a finite positive number")

    generated_at = now.astimezone() if now is not None else datetime.now().astimezone()
    since = generated_at - timedelta(hours=hours)
    items = load_recent_items(
        database_path,
        since=since.timestamp(),
        lifecycle_database_path=lifecycle_database_path,
    )
    content = render_digest(
        items,
        generated_at=generated_at,
        since=since,
        hours=hours,
        show_unverified_translation=show_unverified_translation,
    )

    destination_dir = output_dir.expanduser()
    destination_dir.mkdir(parents=True, exist_ok=True)
    destination = destination_dir / f"{generated_at:%Y-%m-%d}.md"
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            newline="\n",
            dir=destination_dir,
            prefix=f".{destination.name}.",
            suffix=".tmp",
            delete=False,
        ) as temporary_file:
            temporary_path = Path(temporary_file.name)
            temporary_file.write(content)
            temporary_file.flush()
            os.fsync(temporary_file.fileno())
        os.replace(temporary_path, destination)
        temporary_path = None
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)
    return destination, len(items)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="rasool-digest",
        description="Generate a local Markdown digest from the Rasool SQLite feed.",
    )
    parser.add_argument(
        "--database",
        type=Path,
        default=DEFAULT_DATABASE,
        help=f"read-only feed database (default: {DEFAULT_DATABASE})",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=DEFAULT_OUTPUT_DIR,
        help=f"digest directory (default: {DEFAULT_OUTPUT_DIR})",
    )
    parser.add_argument(
        "--lifecycle-database",
        type=Path,
        default=DEFAULT_LIFECYCLE_DATABASE,
        help=(
            "read-only lifecycle database for original summaries "
            f"(default: {DEFAULT_LIFECYCLE_DATABASE})"
        ),
    )
    parser.add_argument(
        "--hours",
        type=float,
        default=24,
        help="look-back window in hours (default: 24)",
    )
    parser.add_argument(
        "--show-unverified-translation",
        action="store_true",
        default=True,
        help="include experimental Arabic summaries marked as unverified",
    )
    parser.add_argument(
        "--hide-unverified-translation",
        action="store_false",
        dest="show_unverified_translation",
        help="omit experimental Arabic summaries from the digest",
    )
    args = parser.parse_args(argv)
    try:
        path, count = generate_digest(
            args.database,
            args.output_dir,
            hours=args.hours,
            lifecycle_database_path=args.lifecycle_database,
            show_unverified_translation=args.show_unverified_translation,
        )
    except (OSError, sqlite3.Error, ValueError) as exc:
        print(f"rasool-digest: {exc}", file=sys.stderr)
        return 1
    print(f"Wrote {path} ({count} items)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
