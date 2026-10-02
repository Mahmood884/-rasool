"""Live keyword search against GitHub (headless, bounded, read-only).

This is the `live_search` capability exposed to Ahmad through rasool_bridge.
It performs a single bounded GitHub repository search for a keyword query and
prints the results as JSON on stdout. It never writes to the feed database and
never executes anything: it is pure read-only intel retrieval.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import httpx

_GITHUB_SEARCH_URL = "https://api.github.com/search/repositories"
_TOKEN_PATH = Path.home() / ".config" / "rasool" / "github_agent" / "token"
_MAX_ALLOWED = 30
_DEFAULT_MAX = 10
_USER_AGENT = "RasoolLiveSearch/0.1.0"
_TIMEOUT_S = 20.0


def _load_token() -> str | None:
    try:
        token = _TOKEN_PATH.read_bytes().decode("utf-8").strip()
    except (FileNotFoundError, OSError, UnicodeDecodeError):
        return None
    return token or None


def _headers() -> dict[str, str]:
    headers = {
        "User-Agent": _USER_AGENT,
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    token = _load_token()
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def search(query: str, max_results: int, sort: str) -> dict:
    per_page = max(1, min(max_results, _MAX_ALLOWED))
    params = {
        "q": query,
        "sort": sort,
        "order": "desc",
        "per_page": per_page,
    }
    with httpx.Client(timeout=_TIMEOUT_S, headers=_headers()) as client:
        response = client.get(_GITHUB_SEARCH_URL, params=params)
    if response.status_code in (403, 429):
        raise RuntimeError(
            "github api rate-limited (unauthenticated 60/hr); "
            "add a token at ~/.config/rasool/github_agent/token"
        )
    response.raise_for_status()
    payload = response.json()
    items: list[dict] = []
    for repo in payload.get("items", []):
        if not isinstance(repo, dict):
            continue
        full_name = repo.get("full_name")
        html_url = repo.get("html_url")
        if not isinstance(full_name, str) or not isinstance(html_url, str):
            continue
        language = repo.get("language")
        items.append(
            {
                "source": "github_live",
                "title": full_name,
                "url": html_url,
                "summary": repo.get("description") or "",
                "stars": repo.get("stargazers_count") or 0,
                "language": language if isinstance(language, str) else None,
            }
        )
    return {"query": query, "count": len(items), "items": items}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="rasool-live-search",
        description="Bounded live GitHub keyword search (read-only).",
    )
    parser.add_argument("--query", required=True, help="GitHub search keywords")
    parser.add_argument(
        "--max",
        type=int,
        default=_DEFAULT_MAX,
        help=f"max results (1-{_MAX_ALLOWED}, default {_DEFAULT_MAX})",
    )
    parser.add_argument(
        "--sort",
        choices=["stars", "updated", "forks", "help-wanted-issues"],
        default="stars",
    )
    args = parser.parse_args(argv)

    query = args.query.strip()
    if not query or len(query) > 200:
        print("invalid query (1-200 chars)", file=sys.stderr)
        return 2
    if not 1 <= args.max <= _MAX_ALLOWED:
        print(f"--max must be between 1 and {_MAX_ALLOWED}", file=sys.stderr)
        return 2

    try:
        result = search(query, args.max, args.sort)
    except (httpx.HTTPError, RuntimeError, ValueError) as exc:
        print(f"live_search failed: {exc}", file=sys.stderr)
        return 1

    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
