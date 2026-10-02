# Phase 10 — Translation Guardrails + Glossary Metadata

## Part A — Analysis of Phase 9.2 output

Analyzed all 15 pre-run feed rows. Classification counts:

| Classification | Count |
|---|---:|
| OK | 0 |
| TRANSLITERATED | 4 |
| WRONG_MEANING | 4 |
| PARTIAL | 5 |
| UNCHANGED | 2 |

Worst examples:

1. `Niko1221/Strata` → `عندما تبدأ في شراء أي سطات، تذكري دائمًا أن تبحث عن القيمة والثروة.`
2. `Albert-Weasker/niubigeo` → `عيسى إيفا`
3. `yetone/magpie` → `ولكن واحدة / الحبيرة`

Best examples:

1. `KKKKhazix/AIHOT` → unchanged; identifier preserved.
2. `NVlabs/SoL-Pi` → unchanged; identifier preserved.
3. `cdyforever/how-to-live-better` → `كيف تعيش بحياة أفضل`; meaning is conveyed, but the identifier is lost.

Full row classifications and notes are in [PHASE10_ANALYSIS.md](./PHASE10_ANALYSIS.md).

## Part B — Identifier protection

Added `rasool/translation_guard.py` and `tests/test_translation_guard.py`; integrated masking, restoration, protected-token logging, and source-text fallback when Ollama alters/removes a placeholder. The fallback was added after the operational run demonstrated that Ollama changed `__GUARD_0__` into Arabic text. SQLite schema was not changed.

- Focused translation guard tests: **6/6 passed**.
- Full suite: **33/33 passed**.
- `py_compile`: passed.
- Offline smoke checks covered glossary matching and URL/repository/version/extension/code/acronym/platform protection.

The operational log recorded the following protected-token counts:

| Item URL | Tokens |
|---|---:|
| `https://github.com/SonicloudTech/sonicloud_opensdk` | 1 |
| `https://github.com/mcncarl/jianying-headless` | 1 |
| `https://github.com/jarrodwatts/jev-trader` | 1 |
| `https://blog.csdn.net/blogdevteam/article/details/126135357` | 0 |
| `https://github.com/vinzdg/codenotch` | 1 |
| `https://blog.csdn.net/blogdevteam/article/details/126135357` | 0 |
| `https://blog.csdn.net/blogdevteam/article/details/126135357` | 0 |
| `https://github.com/Contrastive-LM/CLM` | 1 |

The final source-text fallback passes an offline integration test. The online run was not repeated after that adjustment.

## Part C — Glossary metadata

The glossary was found and loaded with **8 entries**. Validation metadata is attached to the in-memory record only; SQLite schema and persisted feed payload remain unchanged. The two items that reached validation during the operational run each had **0 glossary matches**. The CSDN item that timed out did not reach validation. An offline check confirmed that `النواة` matches the `kernel` glossary entry.

## Part D — Operational run

- Duration: **300.1 seconds**.
- Temperature abort: **No**; maximum externally sampled temperature was **80.0°C**, below the 82°C cutoff.
- Feed rows: **15 before → 17 after** (**2 new rows**).
- Queue rows: **2 → 4**.
- Lifecycle rows: **17 → 21**; `published` **15 → 17**, `retry_wait` **2 → 4**.
- Log: 3 GitHub repository-search request lines, 18 thermal transitions, 3 GitHub-agent starts and stops, and 0 `cancelled:` lines. The application log contained 0 matching `POST /api/generate HTTP 200` lines; this log format does not record successful Ollama HTTP requests, so that count is not evidence that no requests succeeded.

New feed titles:

| URL | Original title | Arabic title | `__GUARD_` leaked? | `github.com` in title? |
|---|---|---|---|---|
| `https://github.com/mcncarl/jianying-headless` | `mcncarl/jianying-headless` | `المراقب رقم 0` | No | No |
| `https://github.com/jarrodwatts/jev-trader` | `jarrodwatts/jev-trader` | `الحرس_0_` | No | No |

Both output titles indicate that Ollama altered the placeholder instead of preserving the repository identifier. The post-run fallback now retains the original field when its placeholder is missing, but this behavior has only been verified offline.

The run produced only two new titles, so a five-new-title comparison was not possible. For context, the worst Phase 9.2 examples were `Niko1221/Strata` → `عندما تبدأ في شراء أي سطات...`, `Albert-Weasker/niubigeo` → `عيسى إيفا`, `yetone/magpie` → `ولكن واحدة / الحبيرة`, `Mak5er/AirCard` → `ماك5ر / ايركارد`, and `TheoLeeCJ/SemIf-OpenJev` → `عولو لي جاي // سيم إف إس فونج إيفج`. The two new titles are also unusable as translations, though their failure is specifically placeholder mutation.

## Deviations

- The first five-minute online run exposed placeholder mutation. The implementation was then hardened to keep the original source field if a protected marker is missing. Focused and full tests pass after this fix, but the operational run was not repeated.
- Only two new feed titles were available for comparison, rather than five.
- Successful Ollama HTTP status lines were not present in the application log; the requested exact log-pattern count is therefore reported as zero recorded lines, not zero successful generation requests.

## Notes

- Permanent `config.toml` invariants remained unchanged: thermal thresholds 78/85/92°C and enabled agents `sniff_agent`, `github_agent`, `router_agent`.
- `router_agent` was not modified.
- The operational process ended at the five-minute timeout (`rc=124`); no Rasool process remained afterward.
- Operational log and thermal samples remain at `/tmp/rasool_phase10.log` and `/tmp/rasool_phase10_thermal.csv`.

## Phase 10.2 — Offline follow-up

After reviewing the feed results, repository identifiers are now handled structurally instead of relying on Ollama to preserve placeholders:

- Identifier-only titles (for example, `mcncarl/jianying-headless`, `shadcn-ui/lint`, `CopilotKit/openmuse`, and `NVlabs/SoL-Pi`) bypass title translation and remain byte-for-byte unchanged.
- For a descriptive title with a trailing repository identifier, only the description is translated; the delimiter and identifier are reattached unchanged.
- Technical tokens in descriptive text are split into structured spans. Only descriptive spans are sent to the translator; protected terms are reinserted directly, not represented by model-visible placeholders.
- Glossary metadata now distinguishes source terms matched to their expected Arabic from unmatched technical terms. It remains in-memory only.

Offline verification after these changes:

- Focused translation-guard tests: **12/12 passed**.
- Full suite: **39/39 passed**.
- `py_compile`: passed.
- Permanent thermal thresholds and enabled-agent list: unchanged.
- Intel feed: remained at **17 rows**; no database writes or operational run were performed for this follow-up.
