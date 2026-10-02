# Phase 11 — Stop Translating Identifiers

## Summary

Phase 11 changed `agent.py` so titles remain verbatim and only summaries of at least 10 characters are translated. Phase 11.1 migrated tests to that contract without further changes to `agent.py`. The full suite passed all 42 tests. The monitored operational run produced two new feed rows; both had `title_ar` exactly equal to `title_original`. The run was interrupted by the external thermal guard after a sample reached 84°C.

## Part A — Phase 11 tests rewritten with fresh agents

The tests use fresh `GitHubAgent` instances rather than the fixture whose `_translate_record` is mocked:

- `test_title_is_not_translated`
- `test_empty_summary_skips_translation`
- `test_short_summary_skips_translation`

Full suite result: **42 tests passed**.

## Part B — Phase 10 tests migrated

The existing Phase 10 integration tests were retained and migrated to the Phase 11 contract:

- `test_descriptive_title_is_preserved_while_summary_is_translated`
- `test_mixed_title_is_preserved_verbatim`
- `test_repository_only_title_is_preserved_and_summary_translated`

Verified that neither `tests/test_github_lifecycle.py` nor `tests/test_translation_guard.py` references `title_translation_status`, `_translate_structured_text`, or `protected_tokens`.

## Part C — Full suite result

- Total: **42**
- Pass: **42**
- Fail: **0**
- Error: **0**

## Part D — Operational run

- `PHASE11_START`: **1790846714**
- `feed_before`: **17**
- `feed_total`: **19**
- `new_during_phase11`: **2**
- New title match / mismatch: **2 / 0**
- Mismatched URLs: **none**
- Duration: **265.2 seconds**
- Aborted: **Yes**, by the external thermal monitor after a sampled reading reached **84.0°C** (abort threshold was 82°C).
- Maximum external temperature: **84.0°C**

New rows (selected by `persisted_at >= PHASE11_START`):

| Status | URL | Original title | `title_ar` |
|---|---|---|---|
| OK | `https://github.com/SonicloudTech/sonicloud_opensdk` | `SonicloudTech/sonicloud_opensdk` | `SonicloudTech/sonicloud_opensdk` |
| OK | `https://github.com/vinzdg/codenotch` | `vinzdg/codenotch` | `vinzdg/codenotch` |

Operational logs recorded 3 GitHub search request lines, 3 GitHub-agent starts and stops, and 0 `cancelled:` lines. The application log contained 0 lines matching `POST /api/generate HTTP 200`; this pattern count does not establish whether Ollama generation requests succeeded because such status lines were not present in this log format.

## Deviations

- The run ended after **265.2 seconds**, rather than reaching five minutes, because the external temperature monitor sent the abort signal. The highest sampled temperature was 84°C, two degrees above the 82°C threshold due to the sampling interval.

## Notes

Phase 11 policy: all titles preserved verbatim; only meaningful
summaries are translated.

Phase 11 modified `agent.py` to implement this policy. Phase 11.1 only migrated tests and did not further modify `agent.py`.

Permanent `config.toml` thermal thresholds and the enabled-agent list were verified unchanged. The operational log and thermal samples remain at `/tmp/rasool_phase11.log` and `/tmp/rasool_phase11_thermal.csv`.
