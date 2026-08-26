"""
因子单测：IC / RankIC / 分层（quantile）回测。

输入：research/panel.py 产出的月度截面面板（parquet）。
方法对齐理论库标准做法：
  - IC：截面上因子值与"下一期"（即下个月末截面）远期收益的 Pearson 相关系数；
  - RankIC：同上但用 Spearman 秩相关，对因子/收益的极端值更稳健，是更常用的评估口径；
  - 分层回测：每个截面按因子值分5档（quintile），等权持有到下一截面，
    看Q5-Q1（或反向）的多空收益是否稳定为正——这是比单纯IC更贴近"能不能用来选股"
    的验收标准。

因子方向：factors.py 里的 factor_* 列已经统一成"数值越大越好"，本模块默认
"高分组"是第5档（分位数最大的一档），多空收益 = Q5均收益 - Q1均收益。

不做未来函数检查是本模块的默认前提假设——真正的PIT正确性保证在 panel.py，
这里只管统计计算。
"""
from __future__ import annotations

import logging

import numpy as np
import pandas as pd
from scipy import stats

logger = logging.getLogger("research.factor_eval")

FACTOR_COLS = [
    "factor_mom_12_1",
    "factor_rev_1m",
    "factor_vol_20",
    "factor_turnover_20",
    "factor_amp_20",
    "factor_bp",
    "factor_ep",
    "factor_sp",
    "factor_size",
    "factor_roe",
    "factor_gross_margin",
    "factor_low_leverage",
    "factor_earnings_growth",
]


def add_forward_return(panel: pd.DataFrame) -> pd.DataFrame:
    """按 symbol 分组，用下一个截面日的 close 算远期收益（下期收益，用于IC评估）。
    面板已按 rebalance_date 等间隔排列，shift(-1) 即"下一个月末"。"""
    out = panel.sort_values(["symbol", "trade_date"]).copy()
    out["close_next"] = out.groupby("symbol")["close"].shift(-1)
    out["date_next"] = out.groupby("symbol")["trade_date"].shift(-1)
    out["fwd_return"] = out["close_next"] / out["close"] - 1
    return out


def _winsorize(s: pd.Series, lower: float = 0.01, upper: float = 0.99) -> pd.Series:
    lo, hi = s.quantile(lower), s.quantile(upper)
    return s.clip(lo, hi)


def compute_ic_series(panel: pd.DataFrame, factor_col: str) -> pd.DataFrame:
    """逐截面日算 IC/RankIC，返回按日期索引的 DataFrame。"""
    rows = []
    for date, grp in panel.groupby("trade_date"):
        sub = grp[[factor_col, "fwd_return"]].dropna()
        if len(sub) < 30:  # 截面样本太少（早期市场规模小）时IC统计意义不大，跳过
            continue
        x = _winsorize(sub[factor_col])
        y = sub["fwd_return"]
        ic = np.corrcoef(x, y)[0, 1]
        rank_ic, _ = stats.spearmanr(sub[factor_col], y)
        rows.append({"trade_date": date, "ic": ic, "rank_ic": rank_ic, "n": len(sub)})
    return pd.DataFrame(rows).set_index("trade_date")


def compute_quantile_returns(panel: pd.DataFrame, factor_col: str, n_quantiles: int = 5) -> pd.DataFrame:
    """逐截面日按因子分档，返回各档等权下期收益，附Q(top)-Q(bottom)多空收益列。"""
    rows = []
    for date, grp in panel.groupby("trade_date"):
        sub = grp[[factor_col, "fwd_return"]].dropna()
        if len(sub) < n_quantiles * 10:
            continue
        try:
            sub = sub.assign(q=pd.qcut(sub[factor_col], n_quantiles, labels=False, duplicates="drop"))
        except ValueError:
            continue
        q_means = sub.groupby("q")["fwd_return"].mean()
        if q_means.index.min() != 0 or q_means.index.max() != n_quantiles - 1:
            continue  # 因子值高度集中导致分档数不足，跳过该截面
        row = {"trade_date": date, **{f"q{i+1}": q_means.get(i, np.nan) for i in range(n_quantiles)}}
        row["long_short"] = q_means.get(n_quantiles - 1) - q_means.get(0)
        rows.append(row)
    return pd.DataFrame(rows).set_index("trade_date")


def summarize_factor(panel: pd.DataFrame, factor_col: str) -> dict:
    ic_df = compute_ic_series(panel, factor_col)
    q_df = compute_quantile_returns(panel, factor_col)
    if ic_df.empty:
        return {"factor": factor_col, "n_periods": 0}

    ic_mean, ic_std = ic_df["ic"].mean(), ic_df["ic"].std()
    rank_ic_mean, rank_ic_std = ic_df["rank_ic"].mean(), ic_df["rank_ic"].std()
    summary = {
        "factor": factor_col,
        "n_periods": len(ic_df),
        "ic_mean": round(ic_mean, 4),
        "ic_ir": round(ic_mean / ic_std, 3) if ic_std else np.nan,
        "rank_ic_mean": round(rank_ic_mean, 4),
        "rank_ic_ir": round(rank_ic_mean / rank_ic_std, 3) if rank_ic_std else np.nan,
        "pct_ic_positive": round((ic_df["ic"] > 0).mean(), 3),
    }
    if not q_df.empty:
        ls_mean = q_df["long_short"].mean()
        ls_std = q_df["long_short"].std()
        summary["long_short_mean_monthly"] = round(ls_mean, 4)
        summary["long_short_annualized"] = round(ls_mean * 12, 4)
        summary["long_short_ir"] = round(ls_mean / ls_std * np.sqrt(12), 3) if ls_std else np.nan
        summary["long_short_t_stat"] = round(
            ls_mean / ls_std * np.sqrt(len(q_df)), 3
        ) if ls_std else np.nan
    return summary


def evaluate_all_factors(panel_path: str = "data/feature_panel.parquet") -> pd.DataFrame:
    panel = pd.read_parquet(panel_path)
    panel = add_forward_return(panel)
    results = []
    for col in FACTOR_COLS:
        if col not in panel.columns:
            continue
        logger.info("评估因子: %s", col)
        results.append(summarize_factor(panel, col))
    return pd.DataFrame(results).sort_values("rank_ic_mean", ascending=False, na_position="last")


if __name__ == "__main__":
    import argparse

    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    parser = argparse.ArgumentParser(description="因子IC/RankIC/分层回测单测")
    parser.add_argument("--panel", type=str, default="data/feature_panel.parquet")
    parser.add_argument("--out", type=str, default="docs/factor_report.csv")
    args = parser.parse_args()

    report = evaluate_all_factors(args.panel)
    pd.set_option("display.width", 200)
    print(report.to_string(index=False))
    report.to_csv(args.out, index=False)
    logger.info("因子报告已写出 %s", args.out)
