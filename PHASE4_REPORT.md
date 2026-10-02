# Phase 4 Report

## PART A findings

### GitHub retry behavior before the changes

The initial queue insert scheduled new rows immediately:

```python
record["src_lang"],
time.time(),
json.dumps(record, ensure_ascii=False),
```

Failed retries used this fixed configured delay (300 seconds by default):

```python
await asyncio.to_thread(
    self._defer_queue_item, url, attempts + 1,
    time.time() + max(
        1, int(self.translation.get("retry_interval_s", 300))
    ),
)
```

There was no maximum-attempts cap, exponential backoff, or per-pass item limit. Therefore, the expected immediate retry on failure was not present in the inspected code; only first insertion was immediate.

### Router login fields

- `GET /login.cgi` returned HTTP 200 with a “Waiting...” page that redirects the browser to `/`, not the login form.
- The root page's visible input names are `txt_Username`, `txt_Password`, and `VerificationCode`. The verification-code element is hidden in the returned markup.
- The page JavaScript submits the credentials using POST names `UserName` and `PassWord`, requests a token from `/asp/GetRandCount.asp`, and sends it as `x.X_HW_Token`. It also sends `Language`. The router mode reported by the page is `COMMON2`; its JavaScript only sends `CheckCode` in `TOT` mode.
- The login POST endpoint is `/login.cgi`. The existing configured POST parameter names (`UserName`, `PassWord`) therefore matched the JavaScript submission names; they are not the DOM input names.

### Sniff-agent capability check

- `id` reported `uid=1000(sol1)` and supplementary group `wheel`.
- `getcap /usr/bin/python3.12` returned no capabilities.
- `/home/sol1/rasool/.venv/bin/python` is a symlink to the uv-managed Python 3.12 interpreter. `getcap` on that resolved interpreter also returned no capabilities.
- No capability was applied. The exact proposed command is:

  ```sh
  sudo setcap cap_net_raw+eip "$(readlink -f /home/sol1/rasool/.venv/bin/python)"
  ```

## PART B changes

- Added `MAX_TRANSLATION_ATTEMPTS = 8` and `MAX_ITEMS_PER_PASS = 5`.
- New queue rows now use `next_retry_at = time.time() + 30`.
- Due-row selection is limited to five items per pass.
- Each failed retry increments attempts. For attempts below eight, the delay is `min(60 * (2 ** attempts), 3600)` seconds. At attempt eight or above, the row stays in SQLite and is rescheduled seven days ahead.
- Before processing due items, the agent sends one GET to `{ollama_url}/api/tags` with a two-second timeout. If that fails and Argos Translate is unavailable, it logs `no translation backend available; skipping retry pass` and leaves queue rows and attempt counts unchanged.
- The SQLite schema was not changed.

## PART C changes

### `agents/router_agent/config.toml`

Added:

```toml
extra_fields = { Language = "english" }
token_field = "x.X_HW_Token"
random_count = "/asp/GetRandCount.asp"
```

The existing `username_field = "UserName"`, `password_field = "PassWord"`, and `login = "/login.cgi"` were retained because those are the actual POST parameter names and endpoint in the router's page script.

### `agents/router_agent/agent.py`

- Added Base64 encoding for the password, matching the router's `base64encode` implementation.
- `_login` now fetches the random-count token, merges configured extra fields, sets the router's language cookie, and includes the token in the POST.
- Enabled HTTP redirects and added detection for the router's “Waiting...” redirect shim. The login response check no longer treats every response whose request URL contains `login.cgi` as a login form.

## PART D results

- Pacman database sync and Ollama installation: **yes** (`ollama` package version `0.33.3-1`).
- `systemctl is-active ollama`: `active`.
- The first immediate API check occurred before Ollama had begun listening and failed to connect. A later API check succeeded.
- `ollama list`:

  ```text
  NAME            ID              SIZE      MODIFIED
  qwen2.5:1.5b    65ec06548149    986 MB    6 minutes ago
  qwen2.5:3b      357c53fb659c    1.9 GB    8 minutes ago
  ```

- The final `/api/tags` response listed both models.

## PART E results

The final observation run lasted the requested 90 seconds. The run log was summarized; raw log output is not included here.

- Logged `agent.error`-producing error phases: `gitee_explore` 1, `juejin_frontend` 1, `zhihu_hot` 1, `bilibili_rank` 1. The EventBus does not log event names directly, so there were no literal `agent.error` strings in the log.
- `github_agent` generated four successful Ollama `/api/generate` responses. There was no literal `intel.item` log line. The queue count decreased from 188 to 187; the code removes a queued row only after `_publish_item` succeeds, so at least one `intel.item` was published on the EventBus.
- `router_agent` did not emit `router.client` during the run. Its router endpoints returned HTTP 200 “Waiting...” redirect-shim pages rather than client-table HTML; login/session authentication was not established.
- `sniff_agent` started. No sniff permission error appeared in the 90-second log; the checked Python interpreter has no `CAP_NET_RAW` capability.
- Thermal transitions observed: NORMAL → WARN (77 °C), WARN → NORMAL (62 °C), NORMAL → WARN (77 °C), WARN → SUSPEND (86 °C), SUSPEND → NORMAL (67 °C).
- Queue sanity check after the run: 187 rows total. The highest-attempt row had `attempts=1`; it was retained in the queue with its computed retry timestamp.
- Rasool had exited after the timeout; no running `rasool/main.py` process remained.
- `python3 -m py_compile` succeeded for the modified `github_agent/agent.py` and `router_agent/agent.py`.

## Deviations

- The router login fields were not changed to the visible DOM names because the router JavaScript explicitly maps those inputs to POST names `UserName` and `PassWord`. Those configured POST names were already correct.
- Router login remained unsuccessful after applying the discovered token, language, encoding, cookie, and redirect behavior. The client/status URLs returned the router's waiting shim, so no client table could be verified and no `router.client` event was emitted.
- The first Ollama API request failed transiently during service startup; subsequent API checks succeeded.

## Notes

- The final run also logged source-fetch errors: Gitee returned HTTP 405, Juejin HTML parsing found no item links, Zhihu returned HTTP 403, and the Bilibili response lacked a `data` object.
- The user-supplied sudo password was not written to a file or included in this report.
