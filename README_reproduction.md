# Reproduction Package

Companion artifacts for the paper *"Paying for Autonomy: Real-Billing Token and Cost
Economics of a Production Desktop LLM Agent."* This package contains the read-only mining
harness, a read-only companion harness for the window-sensitivity check, and two frozen,
de-identified data snapshots. It is released for double-anonymous review and long-term
archival.

## Contents

| File | Role |
|---|---|
| `exp16_cost_snapshot_20261003.json` | Frozen 70-day snapshot (A) — the single source of truth for every headline figure in the paper |
| `exp16_cost_snapshot_20261024.json` | Frozen 91-day snapshot (B) — used **only** for the paper's window-sensitivity check; no headline figure is taken from it |
| `exp16_cost_economics_mining.py` | Read-only mining harness (regenerates a snapshot and figures from a live deployment) |
| `exp16b_window_sensitivity.py` | Read-only companion harness (recomputes the autonomy-attribution share at four window ends) |
| `exp16b_window_sensitivity_20261024.json` | Frozen output of the companion harness (four-cutoff table, weekly shares, structural indicators) |
| `README_reproduction.md` | This file |
| `LICENSE` | Code license (data are released under CC-BY-4.0) |

## Frozen snapshots (authoritative data)

### Snapshot A — 70 days (headline evidence)

| Field | Value |
|---|---|
| File | `exp16_cost_snapshot_20261003.json` |
| sha256 | `8d8e13cd702482576c8aad2ccfd8c12abc998b9ad911dac1b394c1c0512ca657` |
| Bytes | 19,975 |
| frozen_at | 2026-10-03T20:05:20 |
| row_count | 28,311 (`usage_records`) |
| window | 2026-07-25T06:50:15 to 2026-10-03T18:00:56 (70 days) |
| currency profile | mixed CNY/USD/unset (see snapshot) |

### Snapshot B — 91 days (robustness evidence)

| Field | Value |
|---|---|
| File | `exp16_cost_snapshot_20261024.json` |
| sha256 | `e8da15a3da0774bca2267ac090fa00e2ffab9435fd5d6350dfdec65b48fe2e3e` |
| Bytes | 27,066 |
| frozen_at | 2026-10-24T10:25:31 |
| row_count | 39,714 (`usage_records`) |
| window | 2026-07-25T06:50:15 to 2026-10-24T10:15:43 (91 days) |
| new cohort | 31,296 calls / US$679.02 (authoritative cost base, post-2026-08-29 schema) |

**Immutability.** Snapshot A is the sole provenance source for every headline figure in the
paper (all reported shares, distributions, failure rates, and prices). Snapshot B exists for
one purpose only: to show whether the autonomy-attribution share is a structural constant or a
property of where the 70-day window happens to end. The production database keeps growing, so
the paper always cites these frozen snapshots (with their freeze dates) rather than the live
store. B is never permitted to overwrite A.

**De-identification.** For double-anonymous release, the public copies differ from the
internally frozen snapshots only in non-statistical metadata: (1) `db_path`, whose
machine-specific user directory is normalized to `~/.weclaw/history.db`; (2) `paper`, an
internal label replaced by a neutral descriptor; and (3) a handful of provenance-note strings,
from which internal working codenames are removed. Every statistic, count, timestamp, monetary
value, and derived aggregate is unchanged; no reported number is affected. Both harnesses are
de-identified the same way (internal codenames and planning references removed from comments,
docstrings, and print labels); their code logic, SQL queries, aggregations, and read-only
discipline are unchanged, and their `"paper"` output literal is aligned with the de-identified
snapshots so a re-run reproduces the shipped metadata.

## Reproduction

Both harnesses open the production store **read-only** (SQLite `mode=ro` URI), issue only
aggregate `SELECT` / `GROUP BY` queries, and perform **no writes, no locking, no network
access, no API calls, and no side effects**:

```
python exp16_cost_economics_mining.py
python exp16b_window_sensitivity.py
```

**Prerequisites** — a WeClaw production store on the running machine:

- `~/.weclaw/history.db` (`usage_records` + `balance_snapshots`) — required;
- `~/.weclaw/tool_audit.db` (`tool_audit_log`; the cross-store session-join source for the
  cron-vs-main cost attribution) — required by `exp16b_window_sensitivity.py`, optional for
  `exp16_cost_economics_mining.py`; if absent, the latter's autonomy-attribution result
  degrades gracefully rather than failing.

**Snapshot A output.** Re-running `exp16_cost_economics_mining.py` regenerates an equivalent
snapshot `exp16_cost_snapshot_<YYYYMMDD>.json` and figures for the run date. Because of the
documented live-growth drift, values are equivalent to the frozen snapshot but not
byte-identical; the frozen snapshot itself is never overwritten (unless the harness happens to
be re-run on the same day).

**Window-sensitivity output.** `exp16b_window_sensitivity.py` recomputes the new-cohort
attribution share at four cut-offs (2026-10-03, 2026-10-10, 2026-10-17, 2026-10-24), reports
the label-drift check, the weekly share distribution, and the structural indicators of the
extended window, and writes `exp16b_window_sensitivity_<YYYYMMDD>.json` (5,246 bytes in
the frozen copy). Two properties make it reproducible at any later date: the first cut-off is
aligned to snapshot A's actual last row (`2026-10-03T18:00:56`), so the recomputed row must
reproduce A's frozen values digit for digit; and the last cut-off, the weekly shares, and the
structural indicators are all clamped to snapshot B's window end (`2026-10-24T10:15:43`), so
the whole table is stable against subsequent database growth rather than drifting with it.

## Data characteristics and privacy

De-identified by construction: `usage_records` holds no free-text prompts or completions — only
token counts, model keys, timestamps, session identifiers, and pricing metadata. Every artifact
in this package is an aggregate statistic; there is no trace-level user content.

## License

Data: CC-BY-4.0. Code: see `LICENSE`.
