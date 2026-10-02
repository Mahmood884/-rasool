# Rasool Core

IoT agent platform. EventBus + Services + Drivers.

## Structure

- `rasool/`       — runtime primitives
- `agents/`       — one directory per agent
- `supervisor.py` — orchestrates agents, thermal + idle aware
- `thermal.py`    — coretemp reader, 5-state mode
- `idle.py`       — logind IdleHint watcher
- `main.py`       — entrypoint
- `config.toml`   — runtime configuration
- `rasool/intel_feed.py` — SQLite sink for published `intel.item` events

## Install

    ./install.sh

## Run

    ./run.sh --log-level debug

## Systemd (optional)

    systemctl --user daemon-reload
    systemctl --user enable --now rasool.service
    journalctl --user -u rasool.service -f

## Priorities

- sniff_agent  critical
- router_agent normal
- github_agent low

## Thermal thresholds

WARN 70 / THROTTLE 78 / SUSPEND 85 / EMERGENCY 92 / recover 60

## Intel feed

The supervisor starts a local SQLite sink before any agents so published
`intel.item` events are persisted to `~/.cache/rasool/intel_feed.sqlite`.
The database path is configurable under `[intel_feed].path`.
The `intel_feed` table is keyed by a SHA-256 digest of the item URL, so
redelivering an item updates its row rather than creating a duplicate.
`published` means the event was delivered on the in-process EventBus;
`persisted` means the sink committed it to SQLite. Neither status implies
delivery to an external consumer such as a dashboard or Telegram.

Inspect the local feed with:

```sh
sqlite3 ~/.cache/rasool/intel_feed.sqlite \
  "SELECT source, title_ar, url, persisted_at FROM intel_feed ORDER BY persisted_at DESC;"
```

Generate or refresh a local Markdown digest for the last 24 hours, sorted
by score descending:

```sh
rasool-digest
```

The digest is written atomically to
`~/.cache/rasool/digests/YYYY-MM-DD.md`. The command opens the feed database
and lifecycle metadata databases read-only and performs no network requests
or translation. By default, each item shows its original repository title,
source summary, score, link, and experimental Arabic output under the clearly
marked **"محاولة ترجمة آلية — غير مدققة"** label. Pass
`--hide-unverified-translation` to omit the experimental output. Use `--hours`,
`--database`, `--lifecycle-database`, or `--output-dir` to override the defaults.

GitHub summary translation uses the configured DeepSeek chat-completions
endpoint first when `~/.config/rasool/github_agent/deepseek_token` exists and
is non-empty, then falls back to local Ollama if DeepSeek fails. Without that
token file, it uses Ollama directly. Configure endpoint, model, token-file
path, timeout, temperature, token limit, and system prompt in the
`[translation]` section of `agents/github_agent/config.toml`. The token itself
is never written to logs or reports.
