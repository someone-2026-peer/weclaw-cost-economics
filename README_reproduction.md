# Reproduction Package

Companion artifacts for the paper *"Paying for Autonomy: Real-Billing Token and Cost
Economics of a Production Desktop LLM Agent."* This package contains the read-only mining
harness and the frozen, de-identified data snapshot that back every number reported in the
paper. It is released for double-anonymous review and long-term archival.

## Contents

| File | Role |
|---|---|
| `exp16_cost_snapshot_20261003.json` | Frozen, de-identified snapshot — the single source of truth for all reported figures |
| `exp16_cost_economics_mining.py` | Read-only mining harness (regenerates the snapshot and figures from a live deployment) |
| `README_reproduction.md` | This file |
| `LICENSE` | Code license (data are released under CC-BY-4.0) |

## Frozen snapshot (authoritative data)

| Field | Value |
|---|---|
| File | `exp16_cost_snapshot_20261003.json` |
| sha256 | `8d8e13cd702482576c8aad2ccfd8c12abc998b9ad911dac1b394c1c0512ca657` |
| Bytes | 19,975 |
| frozen_at | 2026-10-03T20:05:20 |
| row_count | 28,311 (`usage_records`) |
| window | 2026-07-25T06:50:15 to 2026-10-03T18:00:56 (70 days) |
| currency profile | mixed CNY/USD/unset (see snapshot) |

**Immutability.** The snapshot is the sole provenance source for every figure in the paper.
The production database keeps growing, so the paper always cites this frozen snapshot (with
its freeze date, 2026-10-03) rather than the live store.

**De-identification.** For double-anonymous release, this public copy differs from the
internally frozen snapshot only in non-statistical metadata: (1) `db_path`, whose
machine-specific user directory is normalized to `~/.weclaw/history.db`; (2) `paper`, an
internal label replaced by a neutral descriptor; and (3) a handful of provenance-note strings
under `corrections_vs_plan`, from which internal working codenames are removed. Every
statistic, count, timestamp, monetary value, and derived aggregate is unchanged; no reported
number is affected. The harness is de-identified the same way (internal codenames and planning
references removed from comments, docstrings, and print labels); its code logic, SQL queries,
aggregations, and read-only discipline are unchanged, and its `"paper"` output literal is
aligned with the de-identified snapshot so a re-run reproduces the shipped metadata.

## Reproduction

The harness opens the production store **read-only** (SQLite `mode=ro` URI), issues only
aggregate `SELECT` / `GROUP BY` queries, and performs **no writes, no locking, no network
access, no API calls, and no side effects**:

```
python exp16_cost_economics_mining.py
```

**Prerequisites** — a WeClaw production store on the running machine:

- `~/.weclaw/history.db` (`usage_records` + `balance_snapshots`) — required;
- `~/.weclaw/tool_audit.db` (`tool_audit_log`; the cross-store session-join source for the
  cron-vs-main cost attribution) — optional; if absent, the autonomy-attribution result
  degrades gracefully rather than failing.

**Output.** Re-running regenerates an equivalent snapshot `exp16_cost_snapshot_<YYYYMMDD>.json`
and figures for the run date. Because of the documented live-growth drift, values are
equivalent to the frozen snapshot but not byte-identical; the frozen snapshot itself is never
overwritten (unless the harness happens to be re-run on the same day).

## Data characteristics and privacy

De-identified by construction: `usage_records` holds no free-text prompts or completions — only
token counts, model keys, timestamps, session identifiers, and pricing metadata. Every artifact
in this package is an aggregate statistic; there is no trace-level user content.

## License

Data: CC-BY-4.0. Code: see `LICENSE`.
