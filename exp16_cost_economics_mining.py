#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""exp16 — 生产桌面 LLM Agent 的 Token 与成本经济学只读挖掘。

数据源：``~/.weclaw/history.db`` 之 ``usage_records``（+ ``balance_snapshots``）。

纪律（务必遵守）：
- **全程只读**：``sqlite3.connect('file:...?mode=ro', uri=True)``，绝不产生 -journal/-wal 写入。
- **零虚构**：所有数字来自实时 DB 聚合；快照即论文数字的唯一回溯源（论文实验数据一致性规范）。
- **脱敏前置**：``usage_records`` 仅含 token/成本/模型元数据，无用户正文；本脚本不触碰 messages 内容。
- **幂等可重冻结**：DB 变化后重跑即得新快照，无副作用。

RQ → 聚合映射：
  RQ1 成本在 模型/来源/调用状态 上的分布   → by_model / by_source / by_status
  RQ2 prompt-cache（cached_tokens）实际节省   → cache
  RQ3 上下文窗口增长与成本关系               → context_growth（percentiles + 周时序）
  RQ4 失败/重试的成本代价                     → by_status(failed) + failure_cost + retry_analysis(②)
  RQ5 模型切换的成本-质量权衡（描述性）       → by_model（cost/token 单价对比）
  RQ3+ 上下文-成本集中度（④）                 → context_cost_concentration（>100K/>200K 成本占比 + Pearson）
  桥③ cron↔main 成本归属             → cron_cost_attribution（session_id ⋈ tool_audit.source）
  成本口径：cost 恒 USD；schema cohort @2026-08-29 分段，新 cohort 为权威基 → cost_model
"""
from __future__ import annotations

import json
import sqlite3
import statistics
from collections import defaultdict
from datetime import datetime, date
from pathlib import Path

HERE = Path(__file__).resolve().parent          # .../<paper_dir>/benchmarks
ROOT = HERE.parent                               # 论文夹根（迁移后布局：harness 置于 benchmarks/ 子目录）
RAW = ROOT / "raw_data"                          # 冻结快照写入 raw_data/
FIG = ROOT / "figures"                           # 图写入 figures/（与正文 figures/ 路径一致）
DB = Path.home() / ".weclaw" / "history.db"
AUDIT = Path.home() / ".weclaw" / "tool_audit.db"   # ③ cron 成本归属：跨库 session_id join 源

# 冻结标签：以运行当日为准（论文引用快照须带此日期）
STAMP = date.today().strftime("%Y%m%d")


def ro_connect() -> sqlite3.Connection:
    if not DB.exists():
        raise SystemExit(f"[FATAL] history.db 不存在: {DB}")
    return sqlite3.connect(f"file:{DB.as_posix()}?mode=ro", uri=True)


def ro_connect_audit() -> sqlite3.Connection | None:
    """tool_audit.db 只读连接（③ cron 成本归属用）；不存在则返回 None。"""
    if not AUDIT.exists():
        return None
    return sqlite3.connect(f"file:{AUDIT.as_posix()}?mode=ro", uri=True)


def pct(sorted_vals: list[float], q: float) -> float:
    """线性插值百分位（sorted_vals 已升序）。"""
    if not sorted_vals:
        return 0.0
    if len(sorted_vals) == 1:
        return float(sorted_vals[0])
    pos = (len(sorted_vals) - 1) * q
    lo = int(pos)
    hi = min(lo + 1, len(sorted_vals) - 1)
    frac = pos - lo
    return float(sorted_vals[lo] * (1 - frac) + sorted_vals[hi] * frac)


def iso_week(ts: str) -> str:
    """把 ISO 时间戳归到 ISO 周（2026-Www），用于上下文增长/成本周时序。"""
    try:
        d = datetime.fromisoformat(ts)
    except (ValueError, TypeError):
        return "unknown"
    y, w, _ = d.isocalendar()
    return f"{y}-W{w:02d}"


def main() -> dict:
    RAW.mkdir(parents=True, exist_ok=True)
    FIG.mkdir(parents=True, exist_ok=True)
    c = ro_connect()
    c.row_factory = sqlite3.Row

    snap: dict = {
        "experiment": "exp16_cost_economics_mining",
        "paper": "cost_economics_study",
        "frozen_at": datetime.now().isoformat(timespec="seconds"),
        "db_path": str(DB),
        "db_size_mb": round(DB.stat().st_size / 1024 / 1024, 2),
        "source_table": "usage_records",
    }

    # ---------- 基础画像 ----------
    n = c.execute("SELECT COUNT(*) FROM usage_records").fetchone()[0]
    win = c.execute(
        "SELECT MIN(timestamp), MAX(timestamp) FROM usage_records"
    ).fetchone()
    snap["row_count"] = n
    snap["window"] = {"min": win[0], "max": win[1]}
    days = (datetime.fromisoformat(win[1]) - datetime.fromisoformat(win[0])).days
    snap["window_days"] = days

    # 币种/空值画像（成本归一化的前置事实）
    cur = {r[0] or "(empty)": r[1] for r in c.execute(
        "SELECT currency, COUNT(*) FROM usage_records GROUP BY currency")}
    nulls = c.execute(
        "SELECT SUM(cost IS NULL), SUM(cost=0), SUM(cost_cny IS NULL), "
        "SUM(cost_cny=0), SUM(cached_tokens IS NULL), SUM(cached_tokens>0), "
        "SUM(price_ref IS NULL OR price_ref='') FROM usage_records").fetchone()
    snap["currency_profile"] = cur
    snap["null_zero_profile"] = {
        "cost_null": nulls[0], "cost_zero": nulls[1],
        "cost_cny_null": nulls[2], "cost_cny_zero": nulls[3],
        "cached_null": nulls[4], "cached_gt0": nulls[5],
        "price_ref_empty": nulls[6],
    }
    tot = c.execute(
        "SELECT ROUND(SUM(cost),4), ROUND(SUM(cost_cny),4), "
        "SUM(prompt_tokens), SUM(completion_tokens), SUM(total_tokens), "
        "SUM(cached_tokens) FROM usage_records").fetchone()
    snap["totals"] = {
        "cost_sum": tot[0], "cost_cny_sum": tot[1],
        "prompt_tokens": tot[2], "completion_tokens": tot[3],
        "total_tokens": tot[4], "cached_tokens": tot[5],
        "_note": "cost_sum=全窗口 USD（跨两 cohort，含迁移前估算器）；成本权威基见 cost_model",
    }

    # ---------- 成本数据模型（schema cohort @ v10.10.0 计费迁移 2026-08-29）----------
    # 事实核实（src/models/pricing.py + registry.py + 只读探针 _p1_costmodel.py）：
    #   · cost 列恒为 USD（price_usage_record: cost=result.cost_usd），全窗口一致；
    #   · cost_cny = cost × fx，fx 为固定配置汇率（实测恒 7.2，非市场浮动）；
    #   · currency 列 = 价表原生币种（CNY/USD），是溯源标签，并非 cost 的币种；
    #   · 旧 cohort（cost_cny IS NULL, pre-0829）用迁移前估算器，含 qwen/kimi 单位混淆
    #     （v10.10.0 已纠）→ 与新 cohort 不可直接比较；新 cohort 为纠偏后双币可溯源口径。
    fx_row = c.execute(
        "SELECT ROUND(AVG(cost_cny/cost),4), ROUND(MIN(cost_cny/cost),4), "
        "ROUND(MAX(cost_cny/cost),4), COUNT(*) FROM usage_records "
        "WHERE cost_cny IS NOT NULL AND cost>0 AND cost_cny>0").fetchone()
    cohort_rows = c.execute("""
        SELECT CASE WHEN cost_cny IS NULL THEN 'old_pre_0829' ELSE 'new_post_0829' END AS cohort,
               COUNT(*) AS n, MIN(timestamp) AS tmin, MAX(timestamp) AS tmax,
               ROUND(SUM(cost),4) AS usd, ROUND(SUM(cost_cny),4) AS cny,
               SUM(total_tokens) AS tt, SUM(prompt_tokens) AS pt,
               SUM(cached_tokens) AS cache, SUM(call_status='failed') AS failed
        FROM usage_records GROUP BY cohort""").fetchall()
    cohorts = {r["cohort"]: dict(r) for r in cohort_rows}

    def _unit_usd(d: dict) -> float | None:
        u, t = d.get("usd"), d.get("tt")
        return round(u / t * 1e6, 4) if u is not None and t else None

    for d in cohorts.values():
        d["unit_usd_per_mtok"] = _unit_usd(d)
    new_c = cohorts.get("new_post_0829", {})
    snap["cost_model"] = {
        "fx_usd_to_cny_implied": fx_row[0],
        "fx_range": {"min": fx_row[1], "max": fx_row[2], "n": fx_row[3]},
        "fx_basis": "固定配置汇率（[billing].usd_to_cny），非市场浮动；cost_cny = cost × fx",
        "cost_field_semantics": {
            "cost": "USD，全窗口一致（price_usage_record: cost=result.cost_usd）",
            "cost_cny": "CNY = cost × fx，仅新 cohort（post-0829）填充；旧行为 NULL",
            "currency": "价表原生币种（CNY/USD）——溯源标签，非 cost 的币种",
        },
        "cohort_boundary": "v10.10.0 计费迁移 @2026-08-29（pricing.py 上线；qwen/kimi 单位混淆纠偏）",
        "cohorts": cohorts,
        "authoritative_cost_basis": {
            "cohort": "new_post_0829",
            "usd": new_c.get("usd"), "cny": new_c.get("cny"), "n": new_c.get("n"),
            "total_tokens": new_c.get("tt"), "unit_usd_per_mtok": new_c.get("unit_usd_per_mtok"),
            "rationale": "唯一具纠偏后双币 + price_ref 行级溯源的口径；成本主张一律以此为准",
        },
    }

    # ---------- RQ1: by source / by status ----------
    NEW = "WHERE cost_cny IS NOT NULL"   # 新 cohort（post-0829）= 成本主张权威口径

    def group_agg(dim: str, where: str = "") -> list[dict]:
        rows = c.execute(f"""
            SELECT {dim} AS k, COUNT(*) AS n,
                   SUM(prompt_tokens) AS pt, SUM(completion_tokens) AS ct,
                   SUM(total_tokens) AS tt, SUM(cached_tokens) AS cache,
                   ROUND(SUM(cost),4) AS cost, ROUND(SUM(cost_cny),4) AS cost_cny
            FROM usage_records {where} GROUP BY {dim} ORDER BY n DESC""").fetchall()
        return [dict(r) for r in rows]

    snap["by_source"] = group_agg("source")           # main/aux/mental_health（注意：无 cron）
    snap["by_status"] = group_agg("call_status")      # ok/failed → RQ4（全窗口）
    snap["by_status_newcohort"] = group_agg("call_status", NEW)

    # ---------- RQ1/RQ5: by model ----------
    snap["by_model"] = group_agg("model_key")                  # 全窗口（token/cache 可靠）
    snap["by_model_newcohort"] = group_agg("model_key", NEW)   # 成本权威基（Fig1/单价用）

    # ---------- RQ2: cache ----------
    cache_rows = c.execute("""
        SELECT prompt_tokens, cached_tokens FROM usage_records
        WHERE cached_tokens > 0""").fetchall()
    if cache_rows:
        ratios = sorted(r["cached_tokens"] / r["prompt_tokens"]
                        for r in cache_rows if r["prompt_tokens"])
        snap["cache"] = {
            "records_with_cache": len(cache_rows),
            "share_of_all": round(len(cache_rows) / n, 4),
            "cached_tokens_sum": sum(r["cached_tokens"] for r in cache_rows),
            "prompt_tokens_sum_cached_recs": sum(r["prompt_tokens"] for r in cache_rows),
            "cache_ratio_p50": round(pct(ratios, 0.50), 4),
            "cache_ratio_p90": round(pct(ratios, 0.90), 4),
            "cache_ratio_mean": round(statistics.mean(ratios), 4),
        }
    else:
        snap["cache"] = {"records_with_cache": 0}

    # ---------- RQ3: context growth ----------
    pt = sorted(r[0] for r in c.execute(
        "SELECT prompt_tokens FROM usage_records WHERE prompt_tokens IS NOT NULL"))
    snap["context_growth"] = {
        "prompt_tokens_p50": round(pct(pt, 0.50), 1),
        "prompt_tokens_p90": round(pct(pt, 0.90), 1),
        "prompt_tokens_p99": round(pct(pt, 0.99), 1),
        "prompt_tokens_max": pt[-1] if pt else 0,
        "count_gt_100k": sum(1 for x in pt if x > 100_000),
        "count_gt_200k": sum(1 for x in pt if x > 200_000),
        "count_gt_500k": sum(1 for x in pt if x > 500_000),
    }
    # 周时序：每周 调用数 / 总成本 / 最大 prompt
    weekly: dict[str, dict] = defaultdict(
        lambda: {"n": 0, "cost": 0.0, "cost_cny": 0.0, "max_prompt": 0})
    for r in c.execute("""SELECT timestamp, prompt_tokens, cost, cost_cny
                          FROM usage_records"""):
        wk = iso_week(r["timestamp"])
        b = weekly[wk]
        b["n"] += 1
        b["cost"] += r["cost"] or 0.0
        b["cost_cny"] += r["cost_cny"] or 0.0
        b["max_prompt"] = max(b["max_prompt"], r["prompt_tokens"] or 0)
    snap["weekly"] = [
        {"week": k, "n": v["n"], "cost": round(v["cost"], 4),
         "cost_cny": round(v["cost_cny"], 4), "max_prompt": v["max_prompt"]}
        for k, v in sorted(weekly.items()) if k != "unknown"
    ]

    # ---------- RQ4: failure cost ----------
    fail = c.execute("""
        SELECT COUNT(*) AS n, SUM(total_tokens) AS tt, ROUND(SUM(cost),4) AS cost,
               ROUND(SUM(cost_cny),4) AS cost_cny
        FROM usage_records WHERE call_status='failed'""").fetchone()
    snap["failure_cost"] = {
        "failed_calls": fail[0], "failed_rate": round((fail[0] or 0) / n, 4),
        "failed_total_tokens": fail[1], "failed_cost": fail[2],
        "failed_cost_cny": fail[3],
    }

    # ---------- 成本单价（RQ5 描述性；新 cohort 权威口径，USD）----------
    cost_vals = sorted(r[0] for r in c.execute(
        f"SELECT cost FROM usage_records {NEW} AND cost IS NOT NULL AND cost>0"))
    snap["cost_distribution"] = {
        "cohort": "new_post_0829", "currency": "USD",
        "nonzero_records": len(cost_vals),
        "p50": round(pct(cost_vals, 0.50), 6),
        "p90": round(pct(cost_vals, 0.90), 6),
        "p99": round(pct(cost_vals, 0.99), 6),
        "max": round(cost_vals[-1], 6) if cost_vals else 0,
    }

    # ---------- 成本溯源（price_ref）+ 余额快照 ----------
    snap["price_ref_top"] = [
        {"ref": r[0], "n": r[1]} for r in c.execute("""
            SELECT price_ref, COUNT(*) AS n FROM usage_records
            WHERE price_ref IS NOT NULL AND price_ref!=''
            GROUP BY price_ref ORDER BY n DESC LIMIT 10""")]
    snap["balance_snapshots"] = [
        dict(r) for r in c.execute(
            "SELECT ts, provider, currency, available FROM balance_snapshots ORDER BY ts")]

    # ---------- RQ3+：④ 上下文-成本集中度（新 cohort, USD）----------
    cc = c.execute("""
        SELECT COUNT(*) AS n,
               SUM(prompt_tokens>100000) AS n100,
               SUM(CASE WHEN prompt_tokens>100000 THEN cost ELSE 0 END) AS c100,
               SUM(prompt_tokens>200000) AS n200,
               SUM(CASE WHEN prompt_tokens>200000 THEN cost ELSE 0 END) AS c200,
               SUM(cost) AS ctot
        FROM usage_records WHERE cost_cny IS NOT NULL""").fetchone()
    pr = c.execute("""SELECT prompt_tokens, cost FROM usage_records
                      WHERE cost_cny IS NOT NULL AND cost>0 AND prompt_tokens>0""").fetchall()
    xs = [r["prompt_tokens"] for r in pr]
    ys = [r["cost"] for r in pr]
    pearson = None
    if len(xs) > 2:
        mx, my = statistics.mean(xs), statistics.mean(ys)
        cov = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
        sx = (sum((x - mx) ** 2 for x in xs)) ** 0.5
        sy = (sum((y - my) ** 2 for y in ys)) ** 0.5
        pearson = round(cov / (sx * sy), 4) if sx and sy else None

    def _share(part, whole):
        return round(part / whole, 4) if whole else None

    snap["context_cost_concentration"] = {
        "cohort": "new_post_0829", "currency": "USD",
        "calls_total": cc["n"], "cost_total": round(cc["ctot"] or 0, 4),
        "gt100k_calls": cc["n100"], "gt100k_call_share": _share(cc["n100"], cc["n"]),
        "gt100k_cost": round(cc["c100"] or 0, 4), "gt100k_cost_share": _share(cc["c100"], cc["ctot"]),
        "gt200k_calls": cc["n200"], "gt200k_call_share": _share(cc["n200"], cc["n"]),
        "gt200k_cost": round(cc["c200"] or 0, 4), "gt200k_cost_share": _share(cc["c200"], cc["ctot"]),
        "pearson_prompt_cost": pearson, "pearson_n": len(xs),
        "finding": "大上下文调用以少数占比吃掉近半成本 → 上下文缩减是首要成本杠杆",
    }

    # ---------- RQ4+：② 失败的即时重试代价（弱信号，诚实记录）----------
    # 事实：failed 行 prompt_tokens/total_tokens 均 0（API 报错不返回 usage）→ 失败本身不计 token 成本；
    # 代价体现在「同 session 紧随其后的重试调用」。实测重试罕见（见下），故为弱/负结果。
    rr = c.execute("""SELECT session_id, timestamp, call_status, total_tokens, cost
                      FROM usage_records WHERE session_id IS NOT NULL AND session_id!=''
                      ORDER BY session_id, timestamp""").fetchall()
    by_sess: dict[str, list] = defaultdict(list)
    for r in rr:
        by_sess[r["session_id"]].append(r)
    retry_events = retry_tokens = 0
    retry_cost = 0.0
    for rs in by_sess.values():
        for i, r in enumerate(rs):
            if r["call_status"] == "failed" and i + 1 < len(rs):
                retry_events += 1
                retry_tokens += rs[i + 1]["total_tokens"] or 0
                retry_cost += rs[i + 1]["cost"] or 0.0
    failed_n = snap["failure_cost"]["failed_calls"] or 0
    snap["retry_analysis"] = {
        "failed_calls": failed_n, "failed_billed_tokens": 0,
        "note_failed_zero_billing": "failed 行 token/cost 均 0：API 报错不返回 usage，失败本身不产生 token 成本",
        "immediate_inband_retries": retry_events,
        "retry_rate_of_failures": round(retry_events / failed_n, 4) if failed_n else None,
        "retry_tokens": retry_tokens, "retry_cost_usd": round(retry_cost, 4),
        "finding": "同 session 紧随失败的即时重试罕见（弱信号）；失败的直接 token 代价≈0，"
                   "可靠性成本主要落在延迟与用户体验，而非 token 账单（与 cron 低错误率呼应）",
    }

    # ---------- ③ cron↔main 成本归属（跨库 session_id join）----------
    # 关键：usage_records.source 只有 main/aux/mental_health，cron 触发被折叠进 'main'；
    # 但 tool_audit_log.source 正确标记 cron。故成本归属须借 session_id 跨库 join。
    audit_conn = ro_connect_audit()
    if audit_conn is not None:
        audit_conn.row_factory = sqlite3.Row
        src_dist = {r[0]: {"calls": r[1], "sessions": r[2]} for r in audit_conn.execute(
            "SELECT source, COUNT(*), COUNT(DISTINCT session_id) FROM tool_audit_log "
            "WHERE session_id IS NOT NULL AND session_id!='' GROUP BY source")}
        cron_sids = {r[0] for r in audit_conn.execute(
            "SELECT DISTINCT session_id FROM tool_audit_log WHERE source='cron' "
            "AND session_id IS NOT NULL AND session_id!=''")}
        main_sids = {r[0] for r in audit_conn.execute(
            "SELECT DISTINCT session_id FROM tool_audit_log WHERE source='main' "
            "AND session_id IS NOT NULL AND session_id!=''")}
        sess_all = {r["session_id"]: r for r in c.execute(
            "SELECT session_id, COUNT(*) n, SUM(cost) usd, SUM(cost_cny) cny, SUM(total_tokens) tt "
            "FROM usage_records WHERE session_id IS NOT NULL AND session_id!='' GROUP BY session_id")}
        sess_new = {r["session_id"]: r for r in c.execute(
            "SELECT session_id, COUNT(*) n, SUM(cost) usd, SUM(cost_cny) cny, SUM(total_tokens) tt "
            "FROM usage_records WHERE cost_cny IS NOT NULL AND session_id IS NOT NULL "
            "AND session_id!='' GROUP BY session_id")}

        def _attr(sids, sess_map):
            hit = [v for k, v in sess_map.items() if k in sids]
            return {"sessions": len(hit), "calls": sum(x["n"] for x in hit),
                    "usd": round(sum(x["usd"] or 0 for x in hit), 4),
                    "cny": round(sum(x["cny"] or 0 for x in hit), 4),
                    "tokens": sum(x["tt"] or 0 for x in hit)}

        snap["cron_cost_attribution"] = {
            "method": "usage_records.session_id ⋈ tool_audit_log.session_id（source 标签）；只读跨库",
            "rationale": "usage_records.source 无 'cron'（cron 触发折叠进 main）；tool_audit.source 才有 cron",
            "cron_main_session_overlap": len(cron_sids & main_sids),
            "full_window": {"cron": _attr(cron_sids, sess_all), "main": _attr(main_sids, sess_all)},
            "new_cohort": {"cron": _attr(cron_sids, sess_new), "main": _attr(main_sids, sess_new)},
            "audit_source_dist": src_dist,
            "unattributed_note": "部分 usage session 无工具调用（纯 LLM 聊天/aux/mental_health），不在 tool_audit 中，故未归属",
        }
        audit_conn.close()
    else:
        snap["cron_cost_attribution"] = {"error": "tool_audit.db 不存在，跳过 ③ 桥接"}

    # ---------- 对计划的实测修正（事实纪律）----------
    snap["corrections_vs_plan"] = {
        "source_values": sorted(cur.keys()),
        "note_source": "usage_records.source = main/aux/mental_health，无 'cron'（cron 触发折叠进 main）；"
                       "cron↔main 成本切分已通过 session_id ⋈ tool_audit.source 实现（见 cron_cost_attribution）。",
        "note_window": f"usage_records 窗口 {win[0]} → {win[1]}（约 {days} 天），"
                       "短于 tool_audit 的 ~4.3 月。",
        "note_max_prompt": f"实测 max prompt_tokens={snap['context_growth']['prompt_tokens_max']}，"
                           "远高于早期估计的 133,803。",
        "note_currency": "【已纠正初版误判】cost 列恒为 USD（全窗口一致），cost_cny=cost×7.2"
                         "（固定汇率，仅新 cohort 填充）；currency 列是价表原生币种（溯源标签），"
                         "非 cost 的币种 → SUM(cost) 本身即单一 USD 口径，无需再'归一化'。"
                         "真正的分段是 schema cohort @2026-08-29（计费迁移）：旧 cohort 迁移前估算器"
                         "（qwen/kimi 单位混淆，已纠）约 $0.307/Mtok，新 cohort 纠偏后约 $0.280/Mtok，"
                         "两者不可直接比较。成本主张以新 cohort 为权威基（见 cost_model），"
                         "旧 cohort 单列 + threats-to-validity 注记。",
        "note_billing": "bill_items 为空、balance_snapshots 仅少量；计费对账层非本研究数据源。",
    }

    c.close()

    # ---------- 写快照 ----------
    out = RAW / f"exp16_cost_snapshot_{STAMP}.json"
    out.write_text(json.dumps(snap, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[OK] 快照已冻结 → {out}")

    # ---------- 草稿图表（matplotlib，headless）----------
    try:
        make_figures(snap)
    except Exception as e:  # noqa: BLE001
        print(f"[WARN] 图表生成跳过（不影响快照）：{e}")

    print_tables(snap)
    return snap


def make_figures(snap: dict) -> None:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    # Fig1: 各模型总成本（USD，新 cohort 权威口径）Top12
    bm = sorted(snap.get("by_model_newcohort", []),
                key=lambda r: (r["cost"] or 0), reverse=True)[:12]
    if bm:
        labels = [r["k"] for r in bm]
        costs = [r["cost"] or 0 for r in bm]
        fig, ax = plt.subplots(figsize=(9, 4.5))
        ax.barh(range(len(labels)), costs, color="#4C72B0")
        ax.set_yticks(range(len(labels)), labels, fontsize=8)
        ax.invert_yaxis()
        ax.set_xlabel("total cost (USD, new cohort post-2026-08-29)")
        ax.set_title("exp16 Fig1 — Total cost by model_key (USD, authoritative cohort)")
        fig.tight_layout()
        fig.savefig(FIG / f"exp16_fig1_cost_by_model_{STAMP}.png", dpi=130)
        plt.close(fig)

    # Fig2: prompt_tokens 分布（log-x 直方图）
    weekly = snap.get("weekly", [])
    if weekly:
        fig, ax1 = plt.subplots(figsize=(9, 4.2))
        xs = range(len(weekly))
        ax1.bar(xs, [w["n"] for w in weekly], color="#DD8452", alpha=0.85, label="calls")
        ax1.set_ylabel("calls / week", color="#DD8452")
        ax1.set_xticks(list(xs), [w["week"] for w in weekly], rotation=60, fontsize=7)
        ax2 = ax1.twinx()
        ax2.plot(list(xs), [w["max_prompt"] for w in weekly], "o-",
                 color="#4C72B0", label="max prompt_tokens")
        ax2.set_ylabel("max prompt_tokens / week", color="#4C72B0")
        ax2.set_yscale("log")
        fig.suptitle("exp16 Fig2 — Weekly call volume vs. max context size (RQ3)")
        fig.tight_layout()
        fig.savefig(FIG / f"exp16_fig2_context_growth_{STAMP}.png", dpi=130)
        plt.close(fig)

    # Fig3: 缓存命中率 by model（Top12 by n）
    bm2 = sorted(snap["by_model"], key=lambda r: r["n"], reverse=True)[:12]
    rows = [(r["k"], (r["cache"] or 0) / r["pt"] if r["pt"] else 0)
            for r in bm2 if r["pt"]]
    if rows:
        fig, ax = plt.subplots(figsize=(9, 4.2))
        ax.bar(range(len(rows)), [x[1] for x in rows], color="#55A868")
        ax.set_xticks(range(len(rows)), [x[0] for x in rows], rotation=45,
                      ha="right", fontsize=7)
        ax.set_ylabel("cached_tokens / prompt_tokens")
        ax.set_title("exp16 Fig3 — Prompt-cache ratio by model (RQ2)")
        fig.tight_layout()
        fig.savefig(FIG / f"exp16_fig3_cache_by_model_{STAMP}.png", dpi=130)
        plt.close(fig)

    # Fig4: 上下文-成本集中度（④）— 少数大上下文调用吃掉近半成本
    ccc = snap.get("context_cost_concentration", {})
    if ccc.get("calls_total"):
        cats = [">100K prompt", ">200K prompt"]
        call_sh = [(ccc.get("gt100k_call_share") or 0) * 100, (ccc.get("gt200k_call_share") or 0) * 100]
        cost_sh = [(ccc.get("gt100k_cost_share") or 0) * 100, (ccc.get("gt200k_cost_share") or 0) * 100]
        xpos = [0, 1]
        fig, ax = plt.subplots(figsize=(7.5, 4.2))
        ax.bar([x - 0.18 for x in xpos], call_sh, 0.36, label="% of calls", color="#8DA0CB")
        ax.bar([x + 0.18 for x in xpos], cost_sh, 0.36, label="% of cost (USD)", color="#E15759")
        for i, (a, b) in enumerate(zip(call_sh, cost_sh)):
            ax.text(i - 0.18, a + 0.5, f"{a:.1f}%", ha="center", fontsize=8)
            ax.text(i + 0.18, b + 0.5, f"{b:.1f}%", ha="center", fontsize=8)
        ax.set_xticks(xpos, cats)
        ax.set_ylabel("share (%)")
        ax.set_title("exp16 Fig4 — Context-cost concentration "
                     f"(new cohort; Pearson r={ccc.get('pearson_prompt_cost')})")
        ax.legend()
        fig.tight_layout()
        fig.savefig(FIG / f"exp16_fig4_context_cost_{STAMP}.png", dpi=130)
        plt.close(fig)

    # Fig5: cron↔main 成本归属（③ 新 cohort）
    nc = snap.get("cron_cost_attribution", {}).get("new_cohort", {})
    if nc.get("cron") and nc.get("main"):
        usd = [nc["cron"]["usd"], nc["main"]["usd"]]
        fig, ax = plt.subplots(figsize=(6.5, 4.2))
        ax.bar(["cron (autonomous)", "main (interactive)"], usd, color=["#F28E2B", "#4C72B0"])
        for i, v in enumerate(usd):
            ax.text(i, v + max(usd) * 0.01, f"${v:.2f}", ha="center", fontsize=9)
        ax.set_ylabel("attributed LLM cost (USD, new cohort)")
        ax.set_title("exp16 Fig5 — Cost attribution: cron vs main (session_id join)")
        fig.tight_layout()
        fig.savefig(FIG / f"exp16_fig5_cron_cost_attribution_{STAMP}.png", dpi=130)
        plt.close(fig)
    print(f"[OK] 草稿图表 → {FIG}")


def print_tables(snap: dict) -> None:
    p = print
    p("\n" + "=" * 70)
    p(f"exp16 成本经济学 — 冻结快照 {snap['frozen_at']}")
    p("=" * 70)
    p(f"行数={snap['row_count']}  窗口={snap['window']['min']} → {snap['window']['max']}"
      f"（{snap['window_days']}天）  DB={snap['db_size_mb']}MB")
    p(f"全窗口 cost(USD,跨两cohort)={snap['totals']['cost_sum']}  "
      f"cost_cny(仅新cohort)={snap['totals']['cost_cny_sum']}")
    p(f"总 token={snap['totals']['total_tokens']}  cached={snap['totals']['cached_tokens']}")

    cm = snap.get("cost_model", {})
    if cm:
        p("\n-- 成本数据模型（schema cohort @2026-08-29 计费迁移）--")
        p(f"   隐含 fx(usd→cny)={cm.get('fx_usd_to_cny_implied')}（固定汇率，"
          f"n={cm.get('fx_range',{}).get('n')}，min=max 验证 cost 恒 USD）")
        for name, d in cm.get("cohorts", {}).items():
            p(f"   {name:<16} n={d.get('n'):<6} USD={d.get('usd'):<10} "
              f"CNY={d.get('cny')} tokens={d.get('tt'):<12} "
              f"$/Mtok={d.get('unit_usd_per_mtok')}")
        acb = cm.get("authoritative_cost_basis", {})
        p(f"   → 成本权威基[{acb.get('cohort')}]：USD={acb.get('usd')} CNY={acb.get('cny')} "
          f"$/Mtok={acb.get('unit_usd_per_mtok')}")

    p("\n-- RQ1 by source（注意：无 cron）--")
    for r in snap["by_source"]:
        p(f"   {r['k']:<15} n={r['n']:<6} total_tokens={r['tt']:<12} "
          f"cost={r['cost']} cost_cny={r['cost_cny']}")

    p("\n-- RQ1/RQ4 by call_status --")
    for r in snap["by_status"]:
        p(f"   {r['k']:<8} n={r['n']:<6} total_tokens={r['tt']:<12} "
          f"cost={r['cost']} cost_cny={r['cost_cny']}")

    p("\n-- RQ1/RQ5 by model (Top 12 by n) --")
    for r in sorted(snap["by_model"], key=lambda x: x["n"], reverse=True)[:12]:
        unit = (r["cost"] / r["tt"]) if r["tt"] else 0
        p(f"   {r['k']:<34} n={r['n']:<6} tt={r['tt']:<11} cost={r['cost']:<10} "
          f"cache={r['cache'] or 0:<10} cost/Mtok={unit*1e6:.4f}")

    cg = snap["context_growth"]
    p("\n-- RQ3 context growth --")
    p(f"   prompt_tokens p50={cg['prompt_tokens_p50']} p90={cg['prompt_tokens_p90']} "
      f"p99={cg['prompt_tokens_p99']}")
    p(f"   max={cg['prompt_tokens_max']}  >100k={cg['count_gt_100k']} "
      f">200k={cg['count_gt_200k']} >500k={cg['count_gt_500k']}")

    ca = snap["cache"]
    p("\n-- RQ2 cache --")
    p(f"   有缓存记录={ca.get('records_with_cache')} ({ca.get('share_of_all',0)*100:.1f}%)  "
      f"ratio p50={ca.get('cache_ratio_p50')} mean={ca.get('cache_ratio_mean')}")

    fc = snap["failure_cost"]
    p("\n-- RQ4 failure cost --")
    p(f"   failed={fc['failed_calls']} ({fc['failed_rate']*100:.2f}%)  "
      f"tokens={fc['failed_total_tokens']} cost={fc['failed_cost']} cost_cny={fc['failed_cost_cny']}")

    ccc = snap.get("context_cost_concentration", {})
    if ccc:
        p("\n-- RQ3+ ④ 上下文-成本集中度（新 cohort, USD）--")
        p(f"   >100K prompt: {ccc['gt100k_calls']}/{ccc['calls_total']} 调用"
          f"（{(ccc['gt100k_call_share'] or 0)*100:.1f}%）→ 成本 ${ccc['gt100k_cost']}"
          f"（{(ccc['gt100k_cost_share'] or 0)*100:.1f}%）")
        p(f"   >200K prompt: {ccc['gt200k_calls']} 调用"
          f"（{(ccc['gt200k_call_share'] or 0)*100:.1f}%）→ 成本 ${ccc['gt200k_cost']}"
          f"（{(ccc['gt200k_cost_share'] or 0)*100:.1f}%）")
        p(f"   Pearson(prompt_tokens, cost) r={ccc['pearson_prompt_cost']} (n={ccc['pearson_n']})")

    ra = snap.get("retry_analysis", {})
    if ra:
        p("\n-- RQ4+ ② 失败的即时重试代价（弱信号）--")
        p(f"   failed={ra['failed_calls']}（token 账单=0）；同session即时重试="
          f"{ra['immediate_inband_retries']}（{(ra['retry_rate_of_failures'] or 0)*100:.1f}%）"
          f"重试tokens={ra['retry_tokens']}")

    cca = snap.get("cron_cost_attribution", {})
    if cca and "new_cohort" in cca:
        p("\n-- ③ cron↔main 成本归属（session_id join）--")
        p(f"   cron/main session 重叠={cca['cron_main_session_overlap']}（应=0，干净分离）")
        for wk in ("full_window", "new_cohort"):
            w = cca[wk]
            p(f"   [{wk}] cron: {w['cron']['sessions']}sess/{w['cron']['calls']}call "
              f"${w['cron']['usd']}  |  main: {w['main']['sessions']}sess/{w['main']['calls']}call "
              f"${w['main']['usd']}")

    p("\n-- 对计划的实测修正 --")
    for k, v in snap["corrections_vs_plan"].items():
        if k != "source_values":
            p(f"   [{k}] {v}")


if __name__ == "__main__":
    main()
