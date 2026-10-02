import asyncio
import hashlib
import html
import json
import logging
import re
import sqlite3
import sys
import time
import tomllib
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlparse

from rasool.eventbus import EventBus
from rasool.safety import Safety
from rasool.service import Service
from rasool.translation_guard import (
    is_repository_identifier,
    split_protected,
    split_repository_title,
)

logger = logging.getLogger(__name__)

_SOURCE_URLS = {
    "github_trending": "https://api.github.com/search/repositories",
    "gitee_explore": "https://gitee.com/explore/all",
    "csdn_ai": "https://blog.csdn.net/nav/ai",
    "juejin_frontend": "https://juejin.cn/frontend",
    "zhihu_hot": "https://www.zhihu.com/topic/19551275/hot",
    "bilibili_rank": "https://api.bilibili.com/x/web-interface/ranking/v2",
}
_VOID_TAGS = {
    "area", "base", "br", "col", "embed", "hr", "img", "input", "link",
    "meta", "param", "source", "track", "wbr",
}
_CJK_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff]")
_ARABIC_RE = re.compile(r"[\u0600-\u06ff]")
_TAG_RE = re.compile(r"(?<!\w)[#＃]([\w.+-]{2,32})")
_ITEM_STATUSES = {
    "fetched", "queued", "translated", "published", "failed", "retry_wait"
}


class _HTMLNode:
    def __init__(
        self,
        tag: str,
        attrs: dict[str, str | None],
        parent: "_HTMLNode | None" = None,
    ) -> None:
        self.tag = tag
        self.attrs = attrs
        self.parent = parent
        self.children: list[_HTMLNode] = []
        self.parts: list[str] = []

    def text(self) -> str:
        return " ".join(
            value
            for value in [
                *self.parts,
                *(child.text() for child in self.children),
            ]
            if value
        )

    def descendants(self, tag: str | None = None) -> list["_HTMLNode"]:
        found: list[_HTMLNode] = []
        for child in self.children:
            if tag is None or child.tag == tag:
                found.append(child)
            found.extend(child.descendants(tag))
        return found


class _ArticleParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.root = _HTMLNode("document", {})
        self.stack = [self.root]

    def handle_starttag(
        self, tag: str, attrs: list[tuple[str, str | None]]
    ) -> None:
        node = _HTMLNode(tag, dict(attrs), self.stack[-1])
        self.stack[-1].children.append(node)
        if tag not in _VOID_TAGS:
            self.stack.append(node)

    def handle_startendtag(
        self, tag: str, attrs: list[tuple[str, str | None]]
    ) -> None:
        self.stack[-1].children.append(
            _HTMLNode(tag, dict(attrs), self.stack[-1])
        )

    def handle_endtag(self, tag: str) -> None:
        for index in range(len(self.stack) - 1, 0, -1):
            if self.stack[index].tag == tag:
                del self.stack[index:]
                return

    def handle_data(self, data: str) -> None:
        text = re.sub(r"\s+", " ", data).strip()
        if text:
            self.stack[-1].parts.append(text)


class GitHubAgent(Service):
    name = "github_agent"
    priority = "low"
    MAX_TRANSLATION_ATTEMPTS = 8
    MAX_ITEMS_PER_PASS = 20
    ACTIVE_MODEL = "qwen2.5:1.5b"
    IDLE_MODEL = "qwen2.5:3b"

    def __init__(
        self,
        bus: EventBus,
        config: dict[str, Any] | None = None,
        safety: Safety | None = None,
    ) -> None:
        self._config = config if config is not None else self._load_config()
        super().__init__(bus, self._config)
        self.safety = safety or Safety()
        self.schedule = self._config.get("schedule", {})
        self.sources = self._config.get("sources", {})
        self.watch = self._config.get("watch", {})
        self.translation = self._config.get("translation", {})
        self.dedup = self._config.get("dedup", {})
        self.storage = self._config.get("storage", {})
        self.http_config = self._config.get("http", {})
        self.cache_dir = Path(
            self._expand_path(
                self.storage.get(
                    "cache_dir", "~/.cache/rasool/github_agent"
                )
            )
        )
        self.queue_path = self.cache_dir / "queue.sqlite"
        self.seen_path = self.cache_dir / "seen.json"
        self.interests_path = Path(
            self._expand_path(
                self.storage.get(
                    "interests_file",
                    "~/.config/rasool/github_agent/interests.toml",
                )
            )
        )
        self._tasks: list[asyncio.Task[None]] = []
        self._seen: dict[str, float] = {}
        self._interests: dict[str, float] = {}
        self._host_locks: dict[str, asyncio.Lock] = {}
        self._host_last_request: dict[str, float] = {}
        self._storage_lock = asyncio.Lock()
        self._models_cache: tuple[float, set[str]] | None = None
        self._glossary: dict[str, dict[str, Any]] = {}
        self._is_idle = False
        self._translation_model = self.ACTIVE_MODEL
        self.MAX_ITEMS_PER_PASS = 5
        self.bus.subscribe("system.idle", self._on_idle_event)

    @staticmethod
    def _expand_path(value: str) -> str:
        return str(Path(value).expanduser())

    @staticmethod
    def _load_config() -> dict[str, Any]:
        path = Path(__file__).with_name("config.toml")
        with path.open("rb") as config_file:
            return tomllib.load(config_file)

    @staticmethod
    def _load_glossary() -> dict[str, dict[str, Any]]:
        path = Path.home() / "dictionaries/glossary/rasool_technical_seed.json"
        if not path.exists():
            logger.info("translation glossary not found at %s", path)
            return {}
        try:
            glossary = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning("could not load translation glossary at %s: %s", path, exc)
            return {}
        if not isinstance(glossary, dict) or any(
            not isinstance(term, str) or not isinstance(entry, dict)
            for term, entry in glossary.items()
        ):
            raise ValueError(f"translation glossary at {path} must be an object of entries")
        return glossary

    async def start(self) -> None:
        if self._running:
            return
        self._glossary = self._load_glossary()
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        await asyncio.to_thread(self._initialize_queue)
        await self._load_seen()
        await self._load_interests()
        self._running = True
        self._tasks = [
            asyncio.create_task(self._supervise()),
            asyncio.create_task(self._collection_loop()),
            asyncio.create_task(self._retry_loop()),
        ]
        self._task = self._tasks[1]

    async def _on_idle_event(self, topic: str, payload: Any) -> None:
        if not isinstance(payload, dict) or not isinstance(payload.get("idle"), bool):
            await self._emit_error(
                ValueError("system.idle event must include a boolean 'idle' field"),
                "idle_event",
            )
            return
        self._is_idle = payload["idle"]
        self._translation_model = (
            self.IDLE_MODEL if self._is_idle else self.ACTIVE_MODEL
        )
        self.MAX_ITEMS_PER_PASS = 20 if self._is_idle else 5
        logger.info(
            "translation profile: idle=%s model=%s max_items=%s",
            self._is_idle,
            self._translation_model,
            self.MAX_ITEMS_PER_PASS,
        )

    async def stop(self) -> None:
        self._running = False
        tasks, self._tasks = self._tasks, []
        for task in tasks:
            if not task.done():
                task.cancel()
        for task in tasks:
            try:
                await task
            except asyncio.CancelledError:
                pass
        self._task = None

    async def _collection_loop(self) -> None:
        interval = max(1, int(self.schedule.get("interval_minutes", 60))) * 60
        while self._running:
            try:
                await self._fetch_cycle()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                await self._emit_error(exc, "collection")
            await asyncio.sleep(interval)

    async def _retry_loop(self) -> None:
        interval = max(1, int(self.translation.get("retry_interval_s", 300)))
        while self._running:
            try:
                await self._retry_due_items()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                await self._emit_error(exc, "translation_retry")
            await asyncio.sleep(interval)

    async def _fetch_cycle(self) -> None:
        window_end = self._now()
        enabled = self.sources.get("enabled", list(_SOURCE_URLS))
        if not isinstance(enabled, list):
            await self._emit_error(
                ValueError("sources.enabled must be an array"), "config"
            )
            enabled = list(_SOURCE_URLS)
        batch_start = window_end
        successful_sources: list[str] = []
        items: list[dict[str, Any]] = []
        for source in enabled:
            if not isinstance(source, str):
                await self._emit_error(
                    ValueError(f"Invalid source name: {source!r}"), "config"
                )
                continue
            if source == "github_releases":
                repos = self.watch.get("repos", [])
                if not isinstance(repos, list):
                    await self._emit_error(
                        ValueError("watch.repos must be an array"),
                        "github_releases",
                    )
                    continue
                for repo in repos:
                    try:
                        source_items = await self._fetch_releases(str(repo))
                        items.extend(source_items)
                        successful_sources.append(source)
                    except asyncio.CancelledError:
                        raise
                    except Exception as exc:
                        await self._emit_error(exc, source, repo=str(repo))
                continue
            if source not in _SOURCE_URLS:
                await self._emit_error(
                    ValueError(f"Unknown source: {source}"), source
                )
                continue
            try:
                items.extend(await self._fetch_source(source))
                successful_sources.append(source)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                await self._emit_error(exc, source)

        for item in items:
            try:
                await self._process_item(item)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                await self._emit_error(
                    exc, str(item.get("source", "item")),
                    url=str(item.get("url", "")),
                )
        await self._log_lifecycle_counts()

        event = {
            "count": len(items),
            "sources": list(dict.fromkeys(successful_sources)),
            "window_start": batch_start,
            "window_end": self._now(),
        }
        await self._publish("intel.batch", event)

    async def _fetch_source(self, source: str) -> list[dict[str, Any]]:
        if source == "github_trending":
            return await self._fetch_github_trending()
        url = _SOURCE_URLS[source]
        headers = None
        if source == "gitee_explore":
            headers = {
                "Accept": "text/html",
                "User-Agent": "RasoolGitHubAgent/0.2.0",
            }
        response = await self._get(url, headers=headers)
        if source == "bilibili_rank":
            return self._parse_bilibili(response.json(), source)
        return self._parse_html(source, response.text, url)

    @staticmethod
    def _load_github_token() -> str | None:
        path = Path.home() / ".config" / "rasool" / "github_agent" / "token"
        try:
            token = path.read_bytes().decode("utf-8").strip()
        except (FileNotFoundError, OSError, UnicodeDecodeError):
            return None
        return token or None

    def _github_headers(self) -> dict[str, str]:
        headers = {
            "User-Agent": "RasoolGitHubAgent/0.2.0",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        token = self._load_github_token()
        if token:
            headers["Authorization"] = f"Bearer {token}"
        return headers

    async def _fetch_github_trending(self) -> list[dict[str, Any]]:
        try:
            import httpx
        except ImportError as exc:
            raise RuntimeError("httpx is required by GitHubAgent") from exc
        date_30d_ago = (
            datetime.now(timezone.utc) - timedelta(days=30)
        ).date().isoformat()
        url = "https://api.github.com/search/repositories"
        try:
            response = await self._get(
                url,
                headers=self._github_headers(),
                params={
                    "q": f"created:>{date_30d_ago}",
                    "sort": "stars",
                    "order": "desc",
                    "per_page": 30,
                },
            )
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code in (403, 429):
                logger.warning("github api rate-limited; will retry next cycle")
                return []
            raise
        payload = response.json()
        if not isinstance(payload, dict) or not isinstance(
            payload.get("items"), list
        ):
            raise ValueError("GitHub repository search response has no items array")
        items: list[dict[str, Any]] = []
        for repository in payload["items"]:
            if not isinstance(repository, dict):
                continue
            full_name = repository.get("full_name")
            html_url = repository.get("html_url")
            if not isinstance(full_name, str) or not isinstance(html_url, str):
                continue
            language = repository.get("language")
            items.append(
                {
                    "source": "github_trending",
                    "title": full_name,
                    "url": html_url,
                    "summary": repository.get("description") or "",
                    "raw_lang": "en",
                    "tags": [language] if isinstance(language, str) and language else [],
                }
            )
        return items

    async def _fetch_releases(self, repo: str) -> list[dict[str, Any]]:
        if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repo):
            raise ValueError(f"Invalid GitHub repository name: {repo!r}")
        url = f"https://github.com/{repo}/releases.atom"
        response = await self._get(url, headers=self._github_headers())
        root = ET.fromstring(response.content)
        items: list[dict[str, Any]] = []
        for entry in root.iter():
            if entry.tag.rsplit("}", 1)[-1] != "entry":
                continue
            fields: dict[str, str] = {}
            link = ""
            for node in entry.iter():
                key = node.tag.rsplit("}", 1)[-1]
                if key == "link":
                    link = node.attrib.get("href", link)
                elif node.text and key in {"title", "summary", "content", "updated"}:
                    fields[key] = re.sub(r"\s+", " ", node.text).strip()
            title = fields.get("title", "")
            if not title or not link:
                continue
            summary = fields.get("summary") or fields.get("content", "")
            items.append(
                {
                    "source": "github_releases",
                    "title": title,
                    "url": urljoin(url, link),
                    "summary": self._clean_markup(summary),
                    "raw_lang": self._detect_language(f"{title} {summary}"),
                    "tags": [repo],
                }
            )
        return items

    def _parse_html(
        self, source: str, document: str, base_url: str
    ) -> list[dict[str, Any]]:
        parser = _ArticleParser()
        parser.feed(document)
        parser.close()
        meta_description = ""
        for node in parser.root.descendants("meta"):
            name = (node.attrs.get("name") or node.attrs.get("property") or "").lower()
            if name in {"description", "og:description"}:
                meta_description = str(node.attrs.get("content") or "")
                if meta_description:
                    break

        items: list[dict[str, Any]] = []
        found_urls: set[str] = set()
        for anchor in parser.root.descendants("a"):
            href = anchor.attrs.get("href")
            title = self._clean_markup(anchor.text())
            if not href or not title or not self._include_link(source, str(href)):
                continue
            url = urljoin(base_url, str(href).strip())
            if url in found_urls:
                continue
            found_urls.add(url)
            summary = self._nearby_summary(anchor, title) or meta_description
            items.append(
                {
                    "source": source,
                    "title": title,
                    "url": url,
                    "summary": summary,
                    "raw_lang": self._detect_language(f"{title} {summary}"),
                    "tags": self._extract_tags(f"{title} {summary}"),
                }
            )
        if not items:
            raise ValueError(
                f"Could not parse any item links from {source} HTML "
                f"(response length {len(document)})"
            )
        return items

    @staticmethod
    def _include_link(source: str, href: str) -> bool:
        parsed = urlparse(href)
        path = parsed.path.lower()
        if source == "github_trending":
            return path.count("/") == 2 and not path.startswith(
                ("/sponsors/", "/features/", "/login/", "/signup/")
            )
        if source == "gitee_explore":
            return path.count("/") == 2 and not path.startswith(
                ("/explore/", "/help/", "/login/")
            )
        if source == "csdn_ai":
            return "/article/details/" in path
        if source == "juejin_frontend":
            return "/post/" in path
        if source == "zhihu_hot":
            return (
                "/question/" in path
                or (path.startswith("/p/") and path.count("/") == 2)
                or "/zhuanlan/" in path
            )
        return False

    def _nearby_summary(self, anchor: _HTMLNode, title: str) -> str:
        node = anchor.parent
        best = ""
        depth = 0
        while node is not None and depth < 5:
            text = self._clean_markup(node.text())
            if len(text) > len(best):
                best = text
            classes = str(node.attrs.get("class") or "").lower()
            if node.tag == "article" or any(
                marker in classes for marker in ("card", "item", "entry", "box")
            ):
                break
            node = node.parent
            depth += 1
        if best.casefold().startswith(title.casefold()):
            best = best[len(title):].strip(" -|·")
        return best[:1200]

    def _parse_bilibili(
        self, payload: Any, source: str
    ) -> list[dict[str, Any]]:
        if not isinstance(payload, dict) or not isinstance(
            payload.get("data"), dict
        ):
            raise ValueError("Bilibili response has no data object")
        videos = payload["data"].get("list")
        if not isinstance(videos, list):
            raise ValueError("Bilibili response has no data.list array")
        items: list[dict[str, Any]] = []
        for video in videos:
            if not isinstance(video, dict):
                continue
            bvid = video.get("bvid")
            title = self._clean_markup(str(video.get("title") or ""))
            if not bvid or not title:
                continue
            summary = self._clean_markup(str(video.get("desc") or ""))
            items.append(
                {
                    "source": source,
                    "title": title,
                    "url": f"https://www.bilibili.com/video/{bvid}",
                    "summary": summary,
                    "raw_lang": self._detect_language(f"{title} {summary}"),
                    "tags": self._extract_tags(f"{title} {summary}"),
                }
            )
        return items

    async def _get(
        self,
        url: str,
        headers: dict[str, str] | None = None,
        params: dict[str, str | int] | None = None,
    ) -> Any:
        try:
            import httpx
        except ImportError as exc:
            raise RuntimeError("httpx is required by GitHubAgent") from exc
        await self._throttle(url)
        timeout = float(self.http_config.get("timeout_s", 20))
        request_headers = headers or {
            "User-Agent": str(
                self.http_config.get("user_agent", "RasoolGitHubAgent/0.1.0")
            )
        }
        async with httpx.AsyncClient(
            timeout=timeout, follow_redirects=True, headers=request_headers
        ) as client:
            response = await client.get(url, params=params)
            response.raise_for_status()
            return response

    async def _throttle(self, url: str) -> None:
        host = urlparse(url).netloc.lower()
        lock = self._host_locks.setdefault(host, asyncio.Lock())
        async with lock:
            delay = 1.0 - (time.monotonic() - self._host_last_request.get(host, 0))
            if delay > 0:
                await asyncio.sleep(delay)
            self._host_last_request[host] = time.monotonic()

    async def _process_item(self, item: dict[str, Any]) -> None:
        url = str(item["url"])
        url_hash = hashlib.sha256(url.encode("utf-8")).hexdigest()
        if url_hash in self._seen:
            existing = await asyncio.to_thread(
                self._load_item_state, url_hash
            )
            if existing is not None and existing["status"] != "published":
                await self._record_item_state(existing["metadata"], "published")
            return
        existing = await asyncio.to_thread(self._load_item_state, url_hash)
        if existing is not None and existing["status"] in {
            "queued", "retry_wait", "failed", "published"
        }:
            return
        title = self._clean_markup(str(item.get("title", "")))
        summary = self._clean_markup(str(item.get("summary", "")))
        raw_lang = str(
            item.get("raw_lang")
            or self._detect_language(f"{title} {summary}")
        )
        record = {
            "source": str(item.get("source", "")),
            "url": url,
            "title": title,
            "raw_title": title,
            "raw_text": summary,
            "src_lang": raw_lang,
            "tags": self._normalize_tags(item.get("tags", [])),
            "score": self._score(title, summary),
            "fetched_at": self._now(),
        }
        await self._record_item_state(record, "fetched")
        try:
            translated = await self._translate_record(record)
        except asyncio.CancelledError:
            retry_at = time.time() + 60
            await asyncio.shield(
                self._queue_record(
                    record,
                    last_error="cancelled:shutdown",
                    next_retry_at=retry_at,
                )
            )
            await asyncio.shield(
                self._record_item_state(
                    record,
                    "retry_wait",
                    attempts=0,
                    last_error="cancelled:shutdown",
                    next_retry_at=retry_at,
                    model_used=record.get("model_used"),
                )
            )
            raise
        except Exception as exc:
            await self._record_item_state(
                record,
                "failed",
                last_error=self._safe_error_summary("translation", exc),
                model_used=record.get("model_used"),
            )
            raise
        if translated is None:
            try:
                await self._queue_record(record)
            except asyncio.CancelledError:
                await self._shielded_queue_on_cancel(
                    record, "cancelled:queue_flush"
                )
                raise
            return
        record["glossary_validation"] = self._validate_translation(
            record, translated["title_ar"], translated["summary_ar"]
        )
        logger.info(
            "glossary matches: %d for %s",
            len(record["glossary_validation"]["matched"]),
            record["url"],
        )
        await self._record_item_state(
            record, "translated", model_used=record.get("model_used")
        )
        try:
            await self._publish_item(record, translated)
            await self._record_item_state(
                record, "published", model_used=record.get("model_used")
            )
        except Exception as exc:
            await self._queue_record(
                record,
                last_error=self._safe_error_summary("item_publish", exc),
            )
            await self._emit_error(exc, "item_publish", url=url)
        except asyncio.CancelledError:
            await self._shielded_queue_on_cancel(
                record, "cancelled:publish_flush"
            )
            raise

    async def _translate_record(
        self, record: dict[str, Any]
    ) -> dict[str, str] | None:
        raw_title = record["raw_title"]
        raw_summary = record["raw_text"].strip()

        # العنوان يُحفظ دائماً كما هو (اسم مستودع أو وصف تقني) ولا يُترجم أبداً.
        if len(raw_summary) < 10:
            return {"title_ar": raw_title, "summary_ar": ""}

        if record["src_lang"] == "ar" or self._contains_arabic(raw_summary):
            record["model_used"] = "identity"
            return {"title_ar": raw_title, "summary_ar": raw_summary}

        # يُترجم الملخص فقط.
        summary_ar = await self._translate_local(raw_summary, record["src_lang"], record)
        if summary_ar is None:
            return None

        return {"title_ar": raw_title, "summary_ar": summary_ar}

    async def _translate_structured_text(
        self,
        text: str,
        source_lang: str,
        record: dict[str, Any],
        protected_tokens: list[str],
    ) -> str | None:
        translated_segments: list[str] = []
        for segment, is_protected in split_protected(text):
            if is_protected:
                translated_segments.append(segment)
                protected_tokens.append(segment)
            elif not segment.strip():
                translated_segments.append(segment)
            else:
                translated_segment = await self._translate_local(
                    segment, source_lang, record
                )
                if translated_segment is None:
                    return None
                translated_segments.append(translated_segment)
        return "".join(translated_segments)

    def _validate_translation(
        self,
        record: dict[str, Any],
        title_ar: str,
        summary_ar: str,
    ) -> dict[str, Any]:
        glossary = self._glossary or {}
        matched = []
        unmatched = []
        source_text = (
            f"{record.get('raw_title', '')} {record.get('raw_text', '')}"
        )
        translated_text = f"{title_ar} {summary_ar}".casefold()
        for term_en, entry in glossary.items():
            arabic_term = entry.get("ar", "")
            if not isinstance(arabic_term, str) or not arabic_term:
                continue
            term_pattern = re.compile(
                rf"(?<![A-Za-z0-9]){re.escape(term_en)}(?![A-Za-z0-9])",
                re.IGNORECASE,
            )
            if not term_pattern.search(source_text):
                continue
            if arabic_term.casefold() in translated_text:
                matched.append({
                    "term_en": term_en,
                    "expected_ar": arabic_term,
                    "filter_eligible": bool(entry.get("filter_eligible", False)),
                })
            else:
                unmatched.append(term_en)
        return {
            "matched": matched,
            "unmatched_technical_terms": unmatched,
        }

    async def _translate_local(
        self,
        text: str,
        source_lang: str,
        record: dict[str, Any] | None = None,
    ) -> str | None:
        if not text.strip():
            return ""
        if self.translation.get("deepseek_enabled", True):
            try:
                translated = await self._translate_deepseek(
                    text, source_lang, record
                )
                if translated:
                    return translated
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning(
                    "DeepSeek translation failed (%s); falling back to Ollama",
                    type(exc).__name__,
                )
        try:
            translated = await self._translate_ollama(text, source_lang, record)
            if translated:
                return translated
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            await self._emit_error(exc, "ollama_translation")
        try:
            translated = await asyncio.to_thread(
                self._translate_argos, text, source_lang
            )
            if translated:
                if record is not None:
                    record["model_used"] = f"argos:{source_lang}->ar"
                return translated
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            await self._emit_error(exc, "argos_translation")
        return None

    async def _translate_deepseek(
        self,
        text: str,
        source_lang: str,
        record: dict[str, Any] | None = None,
    ) -> str | None:
        try:
            import httpx
        except ImportError as exc:
            raise RuntimeError("httpx is required for DeepSeek requests") from exc

        token_path = Path(
            self._expand_path(
                str(
                    self.translation.get(
                        "deepseek_token_file",
                        "~/.config/rasool/github_agent/deepseek_token",
                    )
                )
            )
        )
        try:
            api_key = token_path.read_text(encoding="utf-8").strip()
        except FileNotFoundError:
            return None
        if not api_key:
            return None

        endpoint = str(
            self.translation.get(
                "deepseek_url",
                "https://api.deepseek.com/chat/completions",
            )
        )
        model = str(self.translation.get("deepseek_model", "deepseek-flash"))
        timeout = float(self.translation.get("deepseek_timeout_s", 30))
        temperature = float(
            self.translation.get("deepseek_temperature", 0.2)
        )
        max_tokens = int(self.translation.get("deepseek_max_tokens", 512))
        system_prompt = str(
            self.translation.get(
                "deepseek_system_prompt",
                "Translate the user's text into Arabic accurately. "
                "Preserve product names, repository identifiers, URLs, code, "
                "and technical terms. Do not add facts or commentary. "
                "Return only the translation.",
            )
        )
        user_prompt = (
            f"Translate the following {source_lang} text into Arabic:\n\n{text}"
        )
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.post(
                endpoint,
                headers={"Authorization": f"Bearer {api_key}"},
                json={
                    "model": model,
                    "messages": [
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": user_prompt},
                    ],
                    "temperature": temperature,
                    "max_tokens": max_tokens,
                },
            )
            response.raise_for_status()
            payload = response.json()
        if not isinstance(payload, dict):
            raise ValueError("DeepSeek response is not a JSON object")
        choices = payload.get("choices")
        if not isinstance(choices, list) or not choices:
            raise ValueError("DeepSeek response has no completion choices")
        choice = choices[0]
        if not isinstance(choice, dict):
            raise ValueError("DeepSeek completion choice is invalid")
        message = choice.get("message")
        if not isinstance(message, dict):
            raise ValueError("DeepSeek completion message is invalid")
        content = message.get("content")
        if not isinstance(content, str):
            raise ValueError("DeepSeek completion content is not text")
        result = self._clean_markup(content)
        if result and record is not None:
            record["model_used"] = f"deepseek:{model}"
        return result or None

    async def _translate_ollama(
        self,
        text: str,
        source_lang: str,
        record: dict[str, Any] | None = None,
    ) -> str | None:
        try:
            import httpx
        except ImportError as exc:
            raise RuntimeError("httpx is required for local Ollama requests") from exc
        base_url = str(
            self.translation.get("ollama_url", "http://127.0.0.1:11434")
        ).rstrip("/")
        timeout = float(self.translation.get("ollama_timeout_s", 30))
        model = self._translation_model
        fallback = str(
            self.translation.get("fallback_model", "qwen2.5:1.5b")
        )
        models = await self._ollama_models(base_url, timeout)
        if models and not self._model_available(model, models):
            model = fallback
        prompt = (
            f"Translate the following {source_lang} text to Arabic. Return only "
            f"the translation, no commentary.\n\n{text}"
        )
        async with httpx.AsyncClient(timeout=timeout) as client:
            for candidate in dict.fromkeys((model, fallback)):
                response = await client.post(
                    f"{base_url}/api/generate",
                    json={"model": candidate, "prompt": prompt, "stream": False},
                )
                if response.status_code == 404 and candidate != fallback:
                    continue
                response.raise_for_status()
                payload = response.json()
                if not isinstance(payload, dict):
                    raise ValueError("Ollama response is not a JSON object")
                result = self._clean_markup(str(payload.get("response") or ""))
                if result and record is not None:
                    record["model_used"] = candidate
                return result or None
        return None

    async def _ollama_models(self, base_url: str, timeout: float) -> set[str]:
        cached = self._models_cache
        if cached is not None and time.monotonic() - cached[0] < 60:
            return cached[1]
        try:
            import httpx
        except ImportError as exc:
            raise RuntimeError("httpx is required for local Ollama requests") from exc
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.get(f"{base_url}/api/tags")
            response.raise_for_status()
            payload = response.json()
        models: set[str] = set()
        if isinstance(payload, dict) and isinstance(payload.get("models"), list):
            for model in payload["models"]:
                if isinstance(model, dict) and isinstance(model.get("name"), str):
                    models.add(model["name"])
        self._models_cache = (time.monotonic(), models)
        return models

    @staticmethod
    def _model_available(model: str, installed: set[str]) -> bool:
        return any(name == model or name.split(":", 1)[0] == model.split(":", 1)[0]
                   for name in installed)

    @staticmethod
    def _translate_argos(text: str, source_lang: str) -> str | None:
        try:
            import argostranslate.translate
        except ImportError:
            return None
        language = argostranslate.translate.get_translation_from_codes(
            source_lang, "ar"
        )
        if language is None:
            return None
        result = language.translate(text)
        return result.strip() if isinstance(result, str) and result.strip() else None

    async def _queue_record(
        self,
        record: dict[str, Any],
        *,
        attempts: int = 0,
        last_error: str | None = None,
        next_retry_at: float | None = None,
    ) -> None:
        retry_at = next_retry_at or time.time() + 30
        await asyncio.to_thread(
            self._insert_queue_record, record, attempts, retry_at
        )
        await self._record_item_state(
            record,
            "retry_wait" if last_error else "queued",
            attempts=attempts,
            last_error=last_error,
            next_retry_at=retry_at,
            model_used=record.get("model_used"),
        )

    def _insert_queue_record(
        self, record: dict[str, Any], attempts: int, next_retry_at: float
    ) -> None:
        with sqlite3.connect(self.queue_path) as connection:
            connection.execute(
                """
                INSERT INTO translation_queue (
                    url, source, title, raw_text, src_lang, attempts,
                    next_retry_at, metadata_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(url) DO UPDATE SET
                    title=excluded.title,
                    raw_text=excluded.raw_text,
                    metadata_json=excluded.metadata_json,
                    attempts=MAX(translation_queue.attempts, excluded.attempts),
                    next_retry_at=MIN(translation_queue.next_retry_at,
                                      excluded.next_retry_at)
                """,
                (
                    record["url"],
                    record["source"],
                    record["raw_title"],
                    record["raw_text"],
                    record["src_lang"],
                    attempts,
                    next_retry_at,
                    json.dumps(record, ensure_ascii=False),
                ),
            )

    async def _retry_due_items(self) -> None:
        rows = await asyncio.to_thread(
            self._load_due_queue_items, time.time(), self.MAX_ITEMS_PER_PASS
        )
        if not rows:
            await self._log_lifecycle_counts()
            return
        if not await self._translation_backend_available():
            logger.info("no translation backend available; skipping retry pass")
            await self._log_lifecycle_counts()
            return
        for row in rows:
            if not self._running:
                return
            url, attempts, metadata_json = row
            record: dict[str, Any] | None = None
            try:
                record = json.loads(metadata_json)
                translated = await self._translate_record(record)
                if translated is None:
                    raise RuntimeError("No local translation tier is available")
                await self._record_item_state(
                    record, "translated", attempts=attempts,
                    model_used=record.get("model_used"),
                )
                await self._publish_item(record, translated)
                await asyncio.to_thread(self._remove_queue_item, url)
                await self._record_item_state(
                    record, "published", attempts=attempts,
                    model_used=record.get("model_used"),
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                attempts_new = attempts + 1
                failed = attempts_new >= self.MAX_TRANSLATION_ATTEMPTS
                if failed:
                    next_retry = time.time() + 7 * 86400
                else:
                    delay = min(60 * (2 ** attempts), 3600)
                    next_retry = time.time() + delay
                await asyncio.to_thread(
                    self._defer_queue_item, url, attempts_new, next_retry,
                )
                if record is not None:
                    await self._record_item_state(
                        record,
                        "failed" if failed else "retry_wait",
                        attempts=attempts_new,
                        last_error=self._safe_error_summary(
                            "translation_retry", exc
                        ),
                        next_retry_at=next_retry,
                        model_used=record.get("model_used"),
                    )
                await self._emit_error(exc, "translation_retry", url=url)
        await self._log_lifecycle_counts()

    async def _translation_backend_available(self) -> bool:
        try:
            import httpx

            base_url = str(
                self.translation.get("ollama_url", "http://127.0.0.1:11434")
            ).rstrip("/")
            async with httpx.AsyncClient(timeout=2) as client:
                response = await client.get(f"{base_url}/api/tags")
                response.raise_for_status()
            return True
        except Exception:
            try:
                import argostranslate.translate  # noqa: F401
            except ImportError:
                return False
            return True

    def _load_due_queue_items(
        self, now: float, limit: int
    ) -> list[tuple[str, int, str]]:
        with sqlite3.connect(self.queue_path) as connection:
            return list(
                connection.execute(
                    "SELECT url, attempts, metadata_json FROM translation_queue "
                    "WHERE next_retry_at <= ? ORDER BY next_retry_at LIMIT ?",
                    (now, limit),
                )
            )

    def _remove_queue_item(self, url: str) -> None:
        with sqlite3.connect(self.queue_path) as connection:
            connection.execute(
                "DELETE FROM translation_queue WHERE url = ?", (url,)
            )

    def _defer_queue_item(self, url: str, attempts: int, next_retry_at: float) -> None:
        with sqlite3.connect(self.queue_path) as connection:
            connection.execute(
                "UPDATE translation_queue SET attempts = ?, next_retry_at = ? "
                "WHERE url = ?",
                (attempts, next_retry_at, url),
            )

    def _initialize_queue(self) -> None:
        with sqlite3.connect(self.queue_path) as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS translation_queue (
                    url TEXT PRIMARY KEY,
                    source TEXT NOT NULL,
                    title TEXT NOT NULL,
                    raw_text TEXT NOT NULL,
                    src_lang TEXT NOT NULL,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    next_retry_at REAL NOT NULL,
                    metadata_json TEXT NOT NULL
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS item_lifecycle (
                    item_id TEXT PRIMARY KEY,
                    source TEXT NOT NULL,
                    url TEXT NOT NULL,
                    status TEXT NOT NULL CHECK (
                        status IN (
                            'fetched', 'queued', 'translated', 'published',
                            'failed', 'retry_wait'
                        )
                    ),
                    attempts INTEGER NOT NULL DEFAULT 0,
                    last_error TEXT,
                    next_retry_at REAL,
                    model_used TEXT,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    published_at REAL,
                    metadata_json TEXT NOT NULL
                )
                """
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS item_lifecycle_status_updated "
                "ON item_lifecycle(status, updated_at)"
            )

    async def _record_item_state(
        self,
        record: dict[str, Any],
        status: str,
        *,
        attempts: int | None = None,
        last_error: str | None = None,
        next_retry_at: float | None = None,
        model_used: str | None = None,
    ) -> None:
        await asyncio.to_thread(
            self._write_item_state,
            record,
            status,
            attempts,
            last_error,
            next_retry_at,
            model_used,
        )

    def _write_item_state(
        self,
        record: dict[str, Any],
        status: str,
        attempts: int | None,
        last_error: str | None,
        next_retry_at: float | None,
        model_used: str | None,
    ) -> None:
        if status not in _ITEM_STATUSES:
            raise ValueError(f"Invalid item lifecycle status: {status}")
        url = str(record["url"])
        item_id = hashlib.sha256(url.encode("utf-8")).hexdigest()
        now = time.time()
        metadata_json = json.dumps(record, ensure_ascii=False)
        with sqlite3.connect(self.queue_path, timeout=10) as connection:
            previous = connection.execute(
                "SELECT status, attempts, model_used, created_at, published_at "
                "FROM item_lifecycle WHERE item_id = ?",
                (item_id,),
            ).fetchone()
            if (
                previous is not None
                and previous[0] == "published"
                and status != "published"
            ):
                return
            old_attempts = previous[1] if previous is not None else 0
            old_model = previous[2] if previous is not None else None
            created_at = previous[3] if previous is not None else now
            published_at = previous[4] if previous is not None else None
            attempts_value = old_attempts if attempts is None else attempts
            model_value = model_used or record.get("model_used") or old_model
            if status == "published":
                published_at = now
            connection.execute(
                """
                INSERT INTO item_lifecycle (
                    item_id, source, url, status, attempts, last_error,
                    next_retry_at, model_used, created_at, updated_at,
                    published_at, metadata_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(item_id) DO UPDATE SET
                    source = excluded.source,
                    url = excluded.url,
                    status = excluded.status,
                    attempts = excluded.attempts,
                    last_error = excluded.last_error,
                    next_retry_at = excluded.next_retry_at,
                    model_used = excluded.model_used,
                    updated_at = excluded.updated_at,
                    published_at = excluded.published_at,
                    metadata_json = excluded.metadata_json
                """,
                (
                    item_id,
                    str(record.get("source", "")),
                    url,
                    status,
                    attempts_value,
                    last_error,
                    next_retry_at,
                    model_value,
                    created_at,
                    now,
                    published_at,
                    metadata_json,
                ),
            )

    def _load_item_state(self, item_id: str) -> dict[str, Any] | None:
        with sqlite3.connect(self.queue_path, timeout=10) as connection:
            row = connection.execute(
                "SELECT status, metadata_json FROM item_lifecycle WHERE item_id = ?",
                (item_id,),
            ).fetchone()
        if row is None:
            return None
        return {"status": row[0], "metadata": json.loads(row[1])}

    async def _log_lifecycle_counts(self) -> None:
        counts = await asyncio.to_thread(self._load_lifecycle_counts)
        logger.info(
            "github item lifecycle: fetched=%d queued=%d translated=%d "
            "published=%d retry_wait=%d failed=%d",
            *(counts.get(status, 0) for status in (
                "fetched", "queued", "translated", "published",
                "retry_wait", "failed",
            )),
        )

    def _load_lifecycle_counts(self) -> dict[str, int]:
        with sqlite3.connect(self.queue_path, timeout=10) as connection:
            rows = connection.execute(
                "SELECT status, COUNT(*) FROM item_lifecycle GROUP BY status"
            )
            return {status: count for status, count in rows}

    @staticmethod
    def _safe_error_summary(phase: str, exc: Exception) -> str:
        return f"{phase}:{type(exc).__name__}"

    async def _shielded_queue_on_cancel(
        self,
        record: dict[str, Any],
        reason: str,
    ) -> None:
        retry_at = time.time() + 60
        await asyncio.shield(
            self._queue_record(
                record,
                last_error=reason,
                next_retry_at=retry_at,
            )
        )

    async def _load_seen(self) -> None:
        self._seen = await asyncio.to_thread(self._read_seen)
        ttl = max(1, int(self.dedup.get("seen_ttl_days", 30))) * 86400
        cutoff = time.time() - ttl
        self._seen = {key: value for key, value in self._seen.items() if value >= cutoff}
        await self._save_seen()

    def _read_seen(self) -> dict[str, float]:
        try:
            payload = json.loads(self.seen_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}
        except (OSError, json.JSONDecodeError) as exc:
            raise RuntimeError(f"Could not read seen URL database: {exc}") from exc
        if not isinstance(payload, dict):
            raise ValueError("seen.json must contain a JSON object")
        result: dict[str, float] = {}
        for key, value in payload.items():
            if isinstance(key, str) and isinstance(value, (int, float)):
                result[key] = float(value)
        return result

    async def _mark_seen(self, url: str) -> None:
        digest = hashlib.sha256(url.encode("utf-8")).hexdigest()
        async with self._storage_lock:
            self._seen[digest] = time.time()
            payload = json.dumps(self._seen, sort_keys=True)
            await asyncio.to_thread(self._atomic_write, self.seen_path, payload)

    async def _save_seen(self) -> None:
        async with self._storage_lock:
            payload = json.dumps(self._seen, sort_keys=True)
            await asyncio.to_thread(self._atomic_write, self.seen_path, payload)

    @staticmethod
    def _atomic_write(path: Path, content: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(path.suffix + ".tmp")
        temporary.write_text(content, encoding="utf-8")
        temporary.replace(path)

    async def _load_interests(self) -> None:
        try:
            content = await asyncio.to_thread(self.interests_path.read_bytes)
        except FileNotFoundError:
            self._interests = {}
            return
        except OSError as exc:
            await self._emit_error(exc, "interests")
            self._interests = {}
            return
        try:
            payload = tomllib.loads(content.decode("utf-8"))
            keywords = payload.get("keywords", {})
            if not isinstance(keywords, dict):
                raise ValueError("interests.toml [keywords] must be a table")
            self._interests = {
                str(word).casefold(): float(weight)
                for word, weight in keywords.items()
                if isinstance(weight, (int, float))
            }
        except (UnicodeDecodeError, tomllib.TOMLDecodeError, ValueError) as exc:
            await self._emit_error(exc, "interests")
            self._interests = {}

    def _score(self, title: str, summary: str) -> float:
        text = f"{title} {summary}".casefold()
        return float(
            sum(weight for keyword, weight in self._interests.items() if keyword in text)
        )

    @staticmethod
    def _detect_language(text: str) -> str:
        if _ARABIC_RE.search(text):
            return "ar"
        if _CJK_RE.search(text):
            return "zh"
        return "en"

    @staticmethod
    def _contains_arabic(text: str) -> bool:
        return bool(_ARABIC_RE.search(text))

    @staticmethod
    def _extract_tags(text: str) -> list[str]:
        return list(dict.fromkeys(match.group(1) for match in _TAG_RE.finditer(text)))

    @staticmethod
    def _normalize_tags(value: Any) -> list[str]:
        if isinstance(value, str):
            value = [value]
        if not isinstance(value, list):
            return []
        return list(dict.fromkeys(str(tag).strip() for tag in value if str(tag).strip()))

    @staticmethod
    def _clean_markup(value: str) -> str:
        text = re.sub(r"<[^>]*>", " ", html.unescape(value))
        return re.sub(r"\s+", " ", text).strip()

    async def _publish_item(
        self, record: dict[str, Any], translated: dict[str, str]
    ) -> None:
        payload = {
            "source": record["source"],
            "title": record["title"],
            "url": record["url"],
            "summary_ar": translated["summary_ar"],
            "title_ar": translated["title_ar"],
            "raw_lang": record["src_lang"],
            "score": float(record["score"]),
            "fetched_at": record["fetched_at"],
            "tags": record["tags"],
        }
        await self._publish("intel.item", payload)
        await self._mark_seen(record["url"])

    async def _publish(self, topic: str, payload: dict[str, Any]) -> None:
        if not self.safety.check(topic, payload):
            raise RuntimeError(f"Safety gate did not allow event {topic}")
        await self.bus.publish_sync(topic, payload)

    async def _emit_error(
        self, exc: Exception, phase: str, **details: Any
    ) -> None:
        message = f"{self.name} [{phase}]: {type(exc).__name__}: {exc}"
        logger.error(message)
        print(message, file=sys.stderr)
        payload: dict[str, Any] = {
            "name": self.name,
            "phase": phase,
            "error": type(exc).__name__,
            "message": str(exc),
        }
        payload.update(details)
        try:
            if self.safety.check("agent.error", payload):
                await self.bus.publish_sync("agent.error", payload)
        except Exception:
            logger.exception("Could not publish %s error event", self.name)

    @staticmethod
    def _now() -> str:
        return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
