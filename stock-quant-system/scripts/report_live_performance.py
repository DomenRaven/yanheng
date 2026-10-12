"""实盘表现对账：只读打开仓库，对 prediction_log / advice_log 每个快照算事后指标。

输出：docs/live-performance-<日期>.md（表格 + 5 行大白话解读）与 docs/live-performance-latest.json
（页面 `pages/10_实盘表现.py` 直接读）。统计全部在 `research/live_performance.py`，本文件只负责
取数、拼报告、落盘。

用法（app 根目录）：python -m scripts.report_live_performance [--date 2026-10-12]
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import math
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT))

import numpy as np
import pandas as pd

from research.live_performance import (
    HORIZONS,
    PRIMARY_HORIZON,
    advice_outcomes,
    decile_table,
    forward_returns,
    independent_windows,
    price_confound,
    rank_ic,
    trading_dates,
)

DOCS = _ROOT / "docs"
LATEST_JSON = DOCS / "live-performance-latest.json"
POOL_LABEL = {"hs": "沪深", "bj": "北交所", "all": "全市场(旧)"}


def load_inputs(conn) -> dict[str, pd.DataFrame]:
    """只读取数。conn 必须是 read_only=True 打开的连接。"""
    preds = conn.execute(
        """
        SELECT symbol, trade_date, model_run_id, coalesce(pool_id, 'all') AS pool_id,
               pred_score, rank, is_tradable
        FROM prediction_log
        """
    ).df()
    cards = conn.execute(
        """
        SELECT as_of, coalesce(pool_id, 'hs') AS pool_id, symbol, name, action, confidence,
               model_run_id, price_levels_json
        FROM advice_log
        """
    ).df()
    start_candidates = []
    if not preds.empty:
        start_candidates.append(pd.to_datetime(preds["trade_date"]).min())
    if not cards.empty:
        start_candidates.append(pd.to_datetime(cards["as_of"]).min())
    start = min(start_candidates) if start_candidates else pd.Timestamp.today() - pd.Timedelta(days=60)
    start_s = start.date().isoformat()
    quotes = conn.execute(
        "SELECT symbol, trade_date, close FROM daily_quotes WHERE adjust = 'qfq' AND trade_date >= ?",
        [start_s],
    ).df()
    index_quotes = conn.execute(
        "SELECT index_code, trade_date, close FROM index_quotes WHERE trade_date >= ?",
        [start_s],
    ).df()
    universe = conn.execute("SELECT symbol, name, is_st, exchange, board FROM universe").df()
    return {"preds": preds, "cards": cards, "quotes": quotes, "index_quotes": index_quotes, "universe": universe}


def _clean(v):
    if isinstance(v, float) and (math.isnan(v) or math.isinf(v)):
        return None
    if isinstance(v, (np.floating,)):
        f = float(v)
        return None if math.isnan(f) or math.isinf(f) else f
    if isinstance(v, (np.integer,)):
        return int(v)
    if isinstance(v, (np.bool_,)):
        return bool(v)
    if isinstance(v, (pd.Timestamp, dt.date)):
        return str(v)[:10]
    if isinstance(v, dict):
        return {str(k): _clean(x) for k, x in v.items()}
    if isinstance(v, (list, tuple)):
        return [_clean(x) for x in v]
    return v


def build_report(inputs: dict[str, pd.DataFrame]) -> dict:
    preds, cards = inputs["preds"], inputs["cards"]
    quotes, index_quotes, universe = inputs["quotes"], inputs["index_quotes"], inputs["universe"]
    dates = trading_dates(quotes)
    data_end = dates[-1]
    universe = universe.copy()
    universe["symbol"] = universe["symbol"].astype(str)

    snapshots: list[dict] = []
    preds = preds.copy()
    preds["symbol"] = preds["symbol"].astype(str)
    preds["trade_date"] = pd.to_datetime(preds["trade_date"])
    for (pool, tdate, run), grp in preds.groupby(["pool_id", "trade_date", "model_run_id"], sort=True):
        tradable = grp[grp["is_tradable"].fillna(True).astype(bool)].set_index("symbol")
        tradable = tradable[~tradable.index.duplicated(keep="last")]
        item: dict = {
            "pool_id": pool,
            "pool_label": POOL_LABEL.get(pool, pool),
            "trade_date": tdate.date().isoformat(),
            "model_run_id": run,
            "n_total": int(len(grp)),
            "n_tradable": int(len(tradable)),
            "days_after": 0,
            "by_horizon": {},
            "primary_horizon": PRIMARY_HORIZON,
            "primary_effective": 0,
            "confound": None,
            "deciles": [],
        }
        if tdate not in dates:
            item["note"] = "快照日没有行情，跳过"
            snapshots.append(item)
            continue
        fr = forward_returns(quotes, tdate, HORIZONS)
        item["days_after"] = int(fr.days_after)
        joined = tradable.join(fr.frame, how="inner")
        for h in HORIZONS:
            eff = fr.effective[h]
            if eff <= 0:
                item["by_horizon"][str(h)] = {"effective": 0, "coverage": 0.0, "rank_ic": None, "p_value": None, "n": 0}
                continue
            # 只用目标日有真实行情的股票；北交所 09-24 之后多数股票行情没更新，填充值不能算收益
            real = joined[joined[f"real_{h}"].astype(bool)]
            ic = rank_ic(real["pred_score"], real[f"fwd_{h}"])
            item["by_horizon"][str(h)] = {
                "effective": int(eff),
                "coverage": float(real.shape[0] / max(1, joined.shape[0])),
                **ic,
            }
        eff_p = fr.effective[PRIMARY_HORIZON]
        item["primary_effective"] = int(eff_p)
        col = f"fwd_{PRIMARY_HORIZON}"
        real_p = joined[joined[f"real_{PRIMARY_HORIZON}"].astype(bool)] if eff_p > 0 else joined.iloc[0:0]
        item["primary_coverage"] = float(real_p.shape[0] / max(1, joined.shape[0]))
        if eff_p < 3:
            item["note"] = f"快照后只有 {eff_p} 个交易日，暂不做价格混杂诊断"
        elif real_p.shape[0] < 30:
            item["note"] = (
                f"目标日只有 {real_p.shape[0]}/{joined.shape[0]} 只股票有真实行情（行情未更新），不做诊断"
            )
        else:
            item["confound"] = price_confound(
                real_p["pred_score"], real_p[col], real_p["price_asof"], rank=real_p["rank"].astype(float)
            )
            dec = decile_table(real_p["pred_score"], real_p[col], real_p["price_asof"])
            item["deciles"] = dec.to_dict(orient="records")
        snapshots.append(item)

    # 2026-08-24 的卡片来自旧「全市场」模型，pool_id 列是后来迁移补的默认值 'hs'，实际 29/30 是北交所股票。
    # 按同一天同一模型在 prediction_log 里的池来还原：当天该模型只扫过 'all' 池 → 卡片归 'all'。
    cards = cards.copy()
    cards["as_of"] = pd.to_datetime(cards["as_of"])
    day_model_pools = preds.groupby(["trade_date", "model_run_id"])["pool_id"].agg(lambda s: frozenset(s))
    all_only = {key for key, pools in day_model_pools.items() if pools == frozenset({"all"})}
    if all_only:
        keys = list(zip(cards["as_of"], cards["model_run_id"]))
        mask = pd.Series([k in all_only for k in keys], index=cards.index)
        cards.loc[mask, "pool_id"] = "all"

    summary, detail = advice_outcomes(cards, quotes, index_quotes, universe)
    if not detail.empty:
        detail = detail.merge(universe[["symbol", "is_st"]], on="symbol", how="left")
        detail_cols = [
            "as_of", "pool_id", "symbol", "name", "confidence", "price_start", "ret",
            "path_max", "path_min", "hit_tp", "hit_sl", "touched_tp", "touched_sl", "is_st", "end",
        ]
        detail = detail[[c for c in detail_cols if c in detail.columns]]

    snap_dates = {s["trade_date"] for s in snapshots}
    if not summary.empty:
        snap_dates |= set(summary["as_of"])
    snap_dates = sorted(snap_dates)
    indep = independent_windows(snap_dates, dates, window=PRIMARY_HORIZON)

    report = {
        "generated_at": dt.datetime.now().isoformat(timespec="seconds"),
        "data_end": data_end.date().isoformat(),
        "horizons": list(HORIZONS),
        "primary_horizon": PRIMARY_HORIZON,
        "snapshots": snapshots,
        "advice_summary": summary.to_dict(orient="records"),
        "advice_detail": detail.to_dict(orient="records") if not detail.empty else [],
        "independence": indep,
    }
    report["interpretation"] = interpret(report)
    return _clean(report)


def _fmt(v, pct: bool = False, nd: int = 2) -> str:
    if v is None or (isinstance(v, float) and math.isnan(v)):
        return "—"
    if pct:
        return f"{v * 100:+.{nd}f}%"
    return f"{v:.{nd}f}"


def interpret(report: dict) -> list[str]:
    """5 行大白话。只翻译表里的数字。"""
    snaps = [s for s in report["snapshots"] if s.get("confound") and s["confound"].get("n", 0) >= 30]
    raw = [s["confound"]["raw_rank_ic"] for s in snaps]
    res = [s["confound"]["residual_rank_ic"] for s in snaps]
    pf = [s["confound"]["spearman_price_fwd"] for s in snaps]
    adv = [
        r for r in report["advice_summary"]
        if r.get("days_held", 0) and (r.get("n_priced") or 0) >= 5
        and (r.get("n_priced") or 0) >= 0.5 * max(1, r.get("n_cards") or 0)   # 行情覆盖过半才进解读
        and r.get("excess_vs_hs300") is not None
        and not (isinstance(r["excess_vs_hs300"], float) and math.isnan(r["excess_vs_hs300"]))
    ]
    ex = [r["excess_vs_hs300"] for r in adv]
    ind = report["independence"]
    lines = []
    if snaps:
        lines.append(
            f"{len(snaps)} 个有后续行情的快照里，模型分数与之后 {report['primary_horizon']} 日涨跌的排名相关系数"
            f"平均 {np.mean(raw):.2f}（范围 {min(raw):.2f}～{max(raw):.2f}）。"
        )
        lines.append(
            f"同一批快照里，股价本身与之后涨跌的相关系数平均 {np.mean(pf):.2f}，"
            f"说明这段时间低价股整体涨得多；控制价格后残差排名相关系数平均 {np.mean(res):.2f}"
            f"（范围 {min(res):.2f}～{max(res):.2f}），这才是扣掉低价效应后模型自己的区分度。"
        )
    else:
        lines.append("目前还没有走满 3 个交易日的快照，排名相关系数和价格混杂诊断暂时空着。")
    if ex:
        lines.append(
            f"{len(adv)} 组建仓卡片持有到 {report['data_end']}，平均收益比沪深300高 {np.mean(ex) * 100:+.1f} 个百分点"
            f"（范围 {min(ex) * 100:+.1f}～{max(ex) * 100:+.1f}）。"
        )
    else:
        lines.append("建仓卡片还没有可结算的持有区间。")
    stale_pools = sorted(
        {s["pool_label"] for s in report["snapshots"] if s.get("days_after", 0) > 0 and s.get("primary_coverage", 1.0) < 0.8}
    )
    stale_txt = f" {'、'.join(stale_pools)}的快照目标日行情覆盖不足 80%，相关行只用有行情的股票，补灌后要重跑。" if stale_pools else ""
    lines.append(ind["note"] + " 这几组数字互相重叠，按独立样本算只有个位数，不能当成稳定结论。" + stale_txt)
    lines.append(
        "下一步看两件事：残差排名相关系数是否持续为正，以及新快照在价格上涨期是否还能跑赢等权。"
    )
    return lines[:5]


def to_markdown(report: dict, title_date: str) -> str:
    L: list[str] = []
    L.append(f"# 实盘表现对账 {title_date}")
    L.append("")
    L.append(f"生成时间 {report['generated_at']}；行情截止 {report['data_end']}；只读打开仓库，不写库。")
    L.append("统计函数：`research/live_performance.py`；取数与落盘：`scripts/report_live_performance.py`。")
    L.append("")
    L.append("## 一、解读")
    L.append("")
    for i, line in enumerate(report["interpretation"], 1):
        L.append(f"{i}. {line}")
    L.append("")
    L.append("## 二、快照 RankIC（Spearman，按可交易股票）")
    L.append("")
    hz = report["horizons"]
    head = "| 池 | 快照日 | 模型 | n | 后续日数 | " + " | ".join(f"{h}日 IC (p)" for h in hz) + " |"
    L.append(head)
    L.append("|" + "---|" * (5 + len(hz)))
    for s in report["snapshots"]:
        cells = []
        for h in hz:
            b = s["by_horizon"].get(str(h)) or {}
            if not b.get("n"):
                cells.append("—")
            else:
                tag = f"{_fmt(b['rank_ic'])} ({_fmt(b['p_value'], nd=3)}, n={b['n']})"
                if b.get("effective", h) != h:
                    tag += f" 截断{b['effective']}日"
                if (b.get("coverage") or 1.0) < 0.8:
                    tag += f" 覆盖{b['coverage'] * 100:.0f}%"
                cells.append(tag)
        L.append(
            f"| {s['pool_label']} | {s['trade_date']} | `{s['model_run_id']}` | {s['n_tradable']} | {s['days_after']} | "
            + " | ".join(cells) + " |"
        )
    L.append("")
    L.append(f"## 三、价格混杂诊断（{report['primary_horizon']} 日收益，截断到数据末尾）")
    L.append("")
    L.append("| 池 | 快照日 | 模型 | n | 原始 RankIC | ρ(价格, 收益) | ρ(名次, 价格) | 残差 RankIC (p) | 价格五分位内 RankIC 均值 |")
    L.append("|---|---|---|---|---|---|---|---|---|")
    for s in report["snapshots"]:
        c = s.get("confound")
        if not c:
            L.append(f"| {s['pool_label']} | {s['trade_date']} | `{s['model_run_id']}` | — | {s.get('note') or '—'} | | | | |")
            continue
        L.append(
            f"| {s['pool_label']} | {s['trade_date']} | `{s['model_run_id']}` | {c['n']} | {_fmt(c['raw_rank_ic'])} | "
            f"{_fmt(c['spearman_price_fwd'])} | {_fmt(c['spearman_rank_price'])} | "
            f"{_fmt(c['residual_rank_ic'])} ({_fmt(c['p_residual'], nd=3)}) | {_fmt(c['quintile_mean_rank_ic'])} |"
        )
    L.append("")
    L.append("残差 RankIC：分数秩与收益秩分别对价格秩回归后取残差，再算相关（偏 Spearman）。ρ(名次, 价格) 为正表示名次越靠前股价越低。")
    L.append("")
    L.append("## 四、分十档（10 = 分数最高档）")
    L.append("")
    for s in report["snapshots"]:
        if not s.get("deciles"):
            continue
        L.append(f"**{s['pool_label']} @ {s['trade_date']} · `{s['model_run_id']}`**")
        L.append("")
        L.append("| 档 | n | 均值 | 中位数 | ≥+8% | ≤−8% | 均价(元) |")
        L.append("|---|---|---|---|---|---|---|")
        for d in s["deciles"]:
            L.append(
                f"| {d['decile']} | {d['n']} | {_fmt(d['mean_fwd'], pct=True)} | {_fmt(d['median_fwd'], pct=True)} | "
                f"{_fmt(d['hit_tp'], pct=True, nd=0)} | {_fmt(d['hit_sl'], pct=True, nd=0)} | {_fmt(d['mean_price'], nd=1)} |"
            )
        L.append("")
    L.append("## 五、建仓卡片结果（as_of 收盘 → 数据末尾收盘）")
    L.append("")
    L.append("| as_of | 池 | 卡片数(有行情) | 持有日 | 均值 | 中位数 | 期末≥+8% | 期末≤−8% | 路径触及+8% | 路径触及−8% | 沪深300 | 中证1000 | 同池等权 | 超额(沪深300) | 超额(等权) |")
    L.append("|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|")
    for r in report["advice_summary"]:
        L.append(
            f"| {r['as_of']} | {POOL_LABEL.get(r['pool_id'], r['pool_id'])} | {r['n_cards']}({r.get('n_priced') or 0}) | {r['days_held']} | "
            f"{_fmt(r.get('mean_ret'), pct=True)} | {_fmt(r.get('median_ret'), pct=True)} | "
            f"{_fmt(r.get('hit_tp'), pct=True, nd=0)} | {_fmt(r.get('hit_sl'), pct=True, nd=0)} | "
            f"{_fmt(r.get('touched_tp'), pct=True, nd=0)} | {_fmt(r.get('touched_sl'), pct=True, nd=0)} | "
            f"{_fmt(r.get('hs300_ret'), pct=True)} | {_fmt(r.get('csi1000_ret'), pct=True)} | {_fmt(r.get('ew_pool_ret'), pct=True)} | "
            f"{_fmt(r.get('excess_vs_hs300'), pct=True)} | {_fmt(r.get('excess_vs_ew'), pct=True)} |"
        )
    L.append("")
    L.append("收益按 as_of 当日收盘计，没有扣次日开盘价差、滑点和费用；卡片真实执行日是下一交易日。")
    L.append("括号里是期末有真实行情的卡片数，其余卡片行情没更新，不进均值。")
    L.append("")
    L.append("## 六、独立窗口")
    L.append("")
    ind = report["independence"]
    L.append(ind["note"] + f" 入选快照日：{', '.join(ind['chosen_dates'])}。")
    L.append("")
    L.append("## 七、已知局限")
    L.append("")
    L.append("- 价格用前复权收盘，和当日真实成交价在最近除权日后会有少量差异。")
    stale = [s for s in report["snapshots"] if s.get("primary_coverage", 1.0) < 0.8 and s.get("days_after", 0) > 0]
    if stale:
        L.append(
            "- 以下快照目标日行情覆盖不足 80%，相关行只用有真实行情的股票："
            + "；".join(f"{s['pool_label']}@{s['trade_date']} 覆盖 {s['primary_coverage'] * 100:.0f}%" for s in stale)
            + "。北交所 2026-09-24 之后行情只更新了十几只，需要补灌后重跑本报告。"
        )
    L.append("- 残差 RankIC 只控制了价格一个变量，市值、行业等其它混杂没有扣。")
    L.append("- 理论库里没有「名义股价效应」的专门文献，本次用 Barra 中性化的思路做偏相关，属理论缺口，待补文献。")
    L.append("- 快照数量少且相互重叠，所有数字都是描述统计，不构成显著性结论。")
    L.append("")
    return "\n".join(L)


def generate(date: str | None = None) -> tuple[dict, Path]:
    """只读取数 → 算报告 → 写 markdown + latest.json。页面上的「重新计算」也走这里。"""
    from common.db import get_connection

    date = date or dt.date.today().isoformat()
    conn = get_connection(read_only=True)
    try:
        inputs = load_inputs(conn)
    finally:
        conn.close()
    report = build_report(inputs)
    md_path = DOCS / f"live-performance-{date.replace('-', '')}.md"
    md_path.write_text(to_markdown(report, date), encoding="utf-8")
    LATEST_JSON.write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
    return report, md_path


def main() -> int:
    parser = argparse.ArgumentParser(description="实盘表现对账（只读）")
    parser.add_argument("--date", default=dt.date.today().isoformat(), help="报告日期，用于文件名")
    args = parser.parse_args()
    report, md_path = generate(args.date)
    print("\n".join(report["interpretation"]))
    print(f"wrote {md_path}")
    print(f"wrote {LATEST_JSON}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
