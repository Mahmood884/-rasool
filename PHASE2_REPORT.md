# Phase 2 Report

## Scope completed

- Restored error logging in `agents/sniff_agent/agent.py` for scheduled and emitted errors.
- Added `github_agent` with configured collection sources, local translation tiers, interest scoring, a persistent retry queue, and URL deduplication.
- Fixed GitHub seen-store locking so marking a URL seen does not reacquire the held lock. Failed item publication is queued rather than marked seen.
- Added `router_agent` with configurable HG8245W5 login and status/client endpoints, session/client/unknown-device events, and persistent local device observations.
- Set `agents/router_agent/config.toml` permissions to owner-only (`0600`) because the requested configuration contains a router password.

## Files changed or added

Sizes and line counts below were measured after the Python compilation check. The report itself is not included.

| File | Lines | Bytes |
|---|---:|---:|
| `agents/sniff_agent/agent.py` | 263 | 10,066 |
| `agents/github_agent/__init__.py` | 3 | 77 |
| `agents/github_agent/config.toml` | 34 | 580 |
| `agents/github_agent/agent.py` | 851 | 32,422 |
| `agents/github_agent/README.md` | 1 | 408 |
| `agents/github_agent/manifest.toml` | 44 | 3,104 |
| `agents/router_agent/__init__.py` | 3 | 77 |
| `agents/router_agent/config.toml` | 32 | 614 |
| `agents/router_agent/agent.py` | 560 | 20,908 |
| `agents/router_agent/README.md` | 1 | 272 |
| `agents/router_agent/manifest.toml` | 43 | 2,449 |

## Validation performed

`python3 -m py_compile` completed successfully for the `sniff_agent`, `github_agent`, and `router_agent` Python modules and package initializers. The generated `__pycache__` files were removed afterward.

No tests, agents, router requests, or other network requests were run. TOML parsing and router HTML/login behavior were not runtime-validated.

## Environment limitations and deviations

- Python system environment inspected for this phase did not have `httpx`, `feedparser`, `lxml`, `bs4`, `argostranslate`, or `scapy` available. No packages were installed.
- `github_agent` and `router_agent` defer `httpx` imports and report/retry on import failure. They therefore require `httpx` to be installed in their runtime environment before they can make HTTP requests.
- The GitHub agent uses standard-library `html.parser` and `xml.etree.ElementTree` parsing rather than unavailable BeautifulSoup/lxml/feedparser packages.
- Argos translation and Scapy capture remain optional/deferred imports; neither was executed in this phase.
- Router login field names, redirects, client-table markup, and status-page uptime format are based on the supplied specification and remain unverified against the actual router.
- `known_devices.json` is created at runtime under `~/.cache/rasool/router_agent/`; it was not generated because the agent was not run.

## Recommendations (not applied)

- Provide `httpx` and any desired local translation/capture packages through the runtime's chosen dependency manager before running the agents.
- Validate the router's actual login fields, redirects, and HTML response structure during a separately requested runtime verification.
- Consider moving the router password from the TOML file to a protected secret source if configuration policy permits.
