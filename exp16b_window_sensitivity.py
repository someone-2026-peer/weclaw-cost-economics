#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""exp16b — 归属份额的窗口敏感性复核（稳健性补充，只读）。

用途：exp16 的 RQ6（cron vs main 成本归属）为**单一 70 天窗口**的点估计；
本脚本用同一套跨库 session_id join、同一库、同一口径，把窗口末端向后推，
给出归属份额随窗口长度的轨迹，用于判断 62.7% 是结构常数还是窗口样本。

纪律与 exp16 完全一致：
- **全程只读**（``mode=ro`` URI），仅聚合 SELECT，不写库、不加锁、零网络。
- **零虚构**：所有数字来自实时 DB 聚合；本脚本另直读 exp16 的两份冻结快照元数据
  （A 2026-10-03 作第 1 行参照；B 2026-10-24 提供窗口末端），使"冻结值"与
  "重算值"并列可核。
- **脱敏前置**：仅 token/成本/模型元数据，无用户正文。

输出：
  raw_data/exp16b_window_sensitivity_<STAMP>.json   （归档用）
  控制台：周度份额 + 四截止点表

口径说明（必须与正文一致）：
- 成本权威基 = 新 cohort（``cost_cny IS NOT NULL``，post-2026-08-29）。
- 会话→活动标签取自 ``tool_audit_log.source``（usage_records.source 无 cron）。
- 未归属余额 = 总成本 − (cron + main)，即无工具调用的会话（纯聊天/aux/mh）。
- 截止点行用**当前**标签集重算；第 1 行截止时刻对齐快照 A 的实际末行，
  故重算值可与冻结值逐位比对（label_drift 段给出核查结果）。
- 周度份额与结构性指标同样截止于快照 B 的窗口末端，故任意日期重跑数值不变
  （不随生产库继续增长而漂移）。
"""
from __future__ import annotations

import json
import sqlite3
import sys
from collections import defaultdict
from datetime import date, datetime
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

HERE = Path(__file__).resolve().parent          # .../benchmarks（公开包中为包根）
ROOT = HERE.parent                               # 论文夹根
STAMP = date.today().strftime("%Y%m%d")
SNAP_A_NAME = "exp16_cost_snapshot_20261003.json"
SNAP_B_NAME = "exp16_cost_snapshot_20261024.json"
# 内部布局为 <paper>/benchmarks + <paper>/raw_data；公开包内快照与脚本同目录
RAW = (ROOT / "raw_data") if (ROOT / "raw_data" / SNAP_A_NAME).exists() else HERE
DB = Path.home() / ".weclaw" / "history.db"
AUDIT = Path.home() / ".weclaw" / "tool_audit.db"


def load_snapshot(name: str) -> dict | None:
    for cand in (RAW / name, HERE / name):
        if cand.exists():
            return json.loads(cand.read_text(encoding="utf-8"))
    return None


SNAP_B = load_snapshot(SNAP_B_NAME)
# 第 4 行截止 = 快照 B 的实际末行；缺快照时退回冻结日末刻。
# 周度/结构指标同样截止于此，保证任意日期重跑数值不变。
WINDOW_END = ((SNAP_B or {}).get("window") or {}).get("max", "2026-10-24T23:59:59")

CUTOFFS = [
    # 第 1 行刻意对齐快照 A 的实际最后一行（18:00:56），使重算值可与冻结值逐位比对
    ("2026-10-03T18:00:56", "70 d (frozen 2026-10-03)"),
    ("2026-10-10T23:59:59", "77 d"),
    ("2026-10-17T23:59:59", "84 d"),
    (WINDOW_END, "91 d (frozen 2026-10-24)"),
]


def ro(p: Path) -> sqlite3.Connection:
    c = sqlite3.connect(f"file:{p.as_posix()}?mode=ro", uri=True)
    c.row_factory = sqlite3.Row
    return c


def iso_week(ts: str) -> str:
    d = datetime.fromisoformat(ts)
    y, w, _ = d.isocalendar()
    return f"{y}-W{w:02d}"


def main() -> dict:
    RAW.mkdir(parents=True, exist_ok=True)
    c = ro(DB)

    out: dict = {
        "experiment": "exp16b_window_sensitivity",
        "paper": "cost_economics_study",
        "frozen_at": datetime.now().isoformat(timespec="seconds"),
        "source_table": "usage_records",
        "method": "同 exp16：usage_records.session_id ⋈ tool_audit_log.session_id(source)；"
                  "成本权威基=新 cohort(cost_cny IS NOT NULL)",
        "purpose": "检验 RQ6 归属份额（cron 占归属成本）是否随报告窗口长度漂移",
    }

    # ---- 会话标签（跨库）----
    if not AUDIT.exists():
        raise SystemExit(f"[FATAL] tool_audit.db 不存在: {AUDIT}")
    a = ro(AUDIT)
    cron_sids = {r[0] for r in a.execute(
        "SELECT DISTINCT session_id FROM tool_audit_log WHERE source='cron' "
        "AND session_id IS NOT NULL AND session_id!=''")}
    main_sids = {r[0] for r in a.execute(
        "SELECT DISTINCT session_id FROM tool_audit_log WHERE source='main' "
        "AND session_id IS NOT NULL AND session_id!=''")}
    audit_span = a.execute(
        "SELECT MIN(timestamp), MAX(timestamp), COUNT(*) FROM tool_audit_log").fetchone()
    a.close()
    out["audit_store"] = {"min": audit_span[0], "max": audit_span[1],
                          "rows": audit_span[2],
                          "cron_sessions": len(cron_sids),
                          "main_sessions": len(main_sids),
                          "overlap": len(cron_sids & main_sids)}

    # ---- 四截止点：归属份额轨迹（新 cohort）----
    table = []
    for cut, label in CUTOFFS:
        rows = c.execute("""
            SELECT session_id sid, SUM(cost) usd, COUNT(*) n, SUM(total_tokens) tt
            FROM usage_records
            WHERE cost_cny IS NOT NULL AND timestamp <= ?
            GROUP BY session_id""", (cut,)).fetchall()
        tot = sum(r["usd"] or 0 for r in rows)
        cron = sum(r["usd"] or 0 for r in rows if r["sid"] in cron_sids)
        mnm = sum(r["usd"] or 0 for r in rows if r["sid"] in main_sids)
        calls = sum(r["n"] for r in rows)
        attr = cron + mnm
        table.append({
            "cutoff": cut, "label": label, "calls": calls,
            "cost_total_usd": round(tot, 4),
            "cron_usd": round(cron, 4), "main_usd": round(mnm, 4),
            "attributed_usd": round(attr, 4),
            "unattributed_usd": round(tot - attr, 4),
            "cron_share_of_attributed": round(cron / attr, 4) if attr else None,
            "cron_sessions": len({r["sid"] for r in rows if r["sid"] in cron_sids}),
            "main_sessions": len({r["sid"] for r in rows if r["sid"] in main_sids}),
            "cron_calls": sum(r["n"] for r in rows if r["sid"] in cron_sids),
            "main_calls": sum(r["n"] for r in rows if r["sid"] in main_sids),
        })
    out["window_sensitivity"] = table

    # ---- 冻结快照 A 的参照值（不重算，直读，避免与正文 62.7% 打架）----
    snap_a = load_snapshot(SNAP_A_NAME)
    if snap_a is not None:
        sa = snap_a
        cc = sa["cron_cost_attribution"]
        nc = cc["new_cohort"]
        out["frozen_reference_A"] = {
            "snapshot": SNAP_A_NAME, "frozen_at": sa["frozen_at"],
            "row_count": sa["row_count"],
            "new_cohort": {
                "calls": sa["cost_model"]["cohorts"]["new_post_0829"]["n"],
                "cost_total_usd": sa["cost_model"]["cohorts"]["new_post_0829"]["usd"],
                "cron_usd": nc["cron"]["usd"], "main_usd": nc["main"]["usd"],
                "attributed_usd": round(nc["cron"]["usd"] + nc["main"]["usd"], 4),
                "cron_share_of_attributed": round(
                    nc["cron"]["usd"] / (nc["cron"]["usd"] + nc["main"]["usd"]), 4),
                "unattributed_usd": round(
                    sa["cost_model"]["cohorts"]["new_post_0829"]["usd"]
                    - nc["cron"]["usd"] - nc["main"]["usd"], 4),
            },
        }

    # ---- 标签漂移量（解释第 1 行 62.8% vs 冻结 62.7%）----
    drift = 0.0
    drift_sids = []
    usage_max = {r["sid"]: r["mx"] for r in c.execute(
        "SELECT session_id sid, MAX(timestamp) mx FROM usage_records "
        "WHERE cost_cny IS NOT NULL AND timestamp <= '2026-10-03T18:00:56' "
        "GROUP BY session_id")}
    a2 = ro(AUDIT)
    for sid, mn in a2.execute(
            "SELECT session_id, MIN(timestamp) FROM tool_audit_log "
            "WHERE source='cron' AND session_id!='' GROUP BY session_id"):
        if sid in usage_max and sid in cron_sids and mn > "2026-10-03T18:00:56":
            drift_sids.append(sid)
    a2.close()
    if drift_sids:
        q = ",".join("?" * len(drift_sids))
        drift = c.execute(
            f"SELECT COALESCE(SUM(cost),0) FROM usage_records WHERE cost_cny IS NOT NULL "
            f"AND timestamp <= '2026-10-03T18:00:56' AND session_id IN ({q})",
            drift_sids).fetchone()[0]
    out["label_drift"] = {
        "cron_sessions_labeled_after_freeze": len(drift_sids),
        "usd_shifted_into_cron": round(drift or 0, 4),
        "note": "这些会话的 usage 行在冻结日之前，但 cron 标签在冻结日之后才落库，"
                "会把成本从其计入 cron 归属；截止时刻对齐冻结末行后，第 1 行重算值"
                "应与冻结值逐位一致，本段即该项核查。",
    }

    # ---- 周度份额（判断点估计是否处于分布高位）----
    weekly: dict[str, dict] = defaultdict(lambda: {"n": 0, "usd": 0.0,
                                                   "cron": 0.0, "main": 0.0})
    for r in c.execute("""SELECT timestamp ts, session_id sid, cost k
                          FROM usage_records
                          WHERE cost_cny IS NOT NULL AND timestamp <= ?""",
                       (WINDOW_END,)):
        w = weekly[iso_week(r["ts"])]
        w["n"] += 1
        w["usd"] += r["k"] or 0.0
        if r["sid"] in cron_sids:
            w["cron"] += r["k"] or 0.0
        elif r["sid"] in main_sids:
            w["main"] += r["k"] or 0.0
    wk = []
    for k in sorted(weekly):
        v = weekly[k]
        att = v["cron"] + v["main"]
        wk.append({"week": k, "calls": v["n"], "cost_usd": round(v["usd"], 4),
                   "cron_usd": round(v["cron"], 4), "main_usd": round(v["main"], 4),
                   "cron_share": round(v["cron"] / att, 4) if att else None})
    out["weekly_share"] = wk
    shares = sorted(x["cron_share"] for x in wk if x["cron_share"] is not None)
    if shares:
        mid = len(shares) // 2
        med = (shares[mid] if len(shares) % 2 else
               (shares[mid - 1] + shares[mid]) / 2)
        out["weekly_share_summary"] = {
            "weeks": len(shares), "min": shares[0], "max": shares[-1],
            "median": round(med, 4),
            "note": "分母为各周已归属成本（cron+main），不含未归属",
        }

    # ---- 延伸后的结构性指标（供 robustness 叙述）----
    cc = c.execute("""
        SELECT COUNT(*) n, SUM(cost) tot,
               SUM(prompt_tokens>100000) n100,
               SUM(CASE WHEN prompt_tokens>100000 THEN cost ELSE 0 END) c100,
               SUM(prompt_tokens>200000) n200,
               SUM(CASE WHEN prompt_tokens>200000 THEN cost ELSE 0 END) c200,
               SUM(prompt_tokens>400000) n400,
               SUM(CASE WHEN prompt_tokens>400000 THEN cost ELSE 0 END) c400
        FROM usage_records
        WHERE cost_cny IS NOT NULL AND timestamp <= ?""", (WINDOW_END,)).fetchone()
    out["extension_structure"] = {
        "calls": cc["n"], "cost_usd": round(cc["tot"], 4),
        "gt100k_calls": cc["n100"], "gt100k_call_share": round(cc["n100"] / cc["n"], 4),
        "gt100k_cost_share": round(cc["c100"] / cc["tot"], 4),
        "gt200k_calls": cc["n200"], "gt200k_call_share": round(cc["n200"] / cc["n"], 4),
        "gt200k_cost_share": round(cc["c200"] / cc["tot"], 4),
        "gt400k_calls": cc["n400"], "gt400k_cost_share": round(cc["c400"] / cc["tot"], 4),
    }

    c.close()
    p = RAW / f"exp16b_window_sensitivity_{STAMP}.json"
    p.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")

    # ---- 控制台表 ----
    print("=" * 78)
    print(f"exp16b 窗口敏感性 — {out['frozen_at']}")
    print("=" * 78)
    print(f"{'window':<26}{'calls':>7}{'total$':>10}{'cron$':>9}{'main$':>9}"
          f"{'unattr$':>9}{'cron%':>7}")
    for r in table:
        print(f"{r['label']:<26}{r['calls']:>7}{r['cost_total_usd']:>10.2f}"
              f"{r['cron_usd']:>9.2f}{r['main_usd']:>9.2f}"
              f"{r['unattributed_usd']:>9.2f}"
              f"{(r['cron_share_of_attributed'] or 0) * 100:>7.1f}")
    fr = out.get("frozen_reference_A", {}).get("new_cohort", {})
    if fr:
        print(f"\n[frozen A 直读] calls={fr['calls']} total=${fr['cost_total_usd']} "
              f"cron=${fr['cron_usd']} main=${fr['main_usd']} "
              f"share={fr['cron_share_of_attributed'] * 100:.1f}%")
    ld = out["label_drift"]
    print(f"[label drift] cron 标签晚于冻结日的会话 {ld['cron_sessions_labeled_after_freeze']} 个，"
          f"涉及 ${ld['usd_shifted_into_cron']}")
    ws = out["weekly_share_summary"]
    print(f"[weekly] {ws['weeks']} 周，share min={ws['min'] * 100:.1f}% "
          f"median={ws['median'] * 100:.1f}% max={ws['max'] * 100:.1f}%")
    es = out["extension_structure"]
    print(f"[structure] >100K {es['gt100k_call_share'] * 100:.1f}% calls / "
          f"{es['gt100k_cost_share'] * 100:.1f}% cost | "
          f">200K {es['gt200k_call_share'] * 100:.1f}% / "
          f"{es['gt200k_cost_share'] * 100:.1f}% cost | "
          f">400K {es['gt400k_calls']} calls / {es['gt400k_cost_share'] * 100:.1f}% cost")
    print(f"\n[OK] -> {p}")
    return out


if __name__ == "__main__":
    main()
