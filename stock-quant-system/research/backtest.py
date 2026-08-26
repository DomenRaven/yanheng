"""
回测引擎：把 train_lightgbm.py 产出的样本外(walk-forward OOS)预测分数转成
"每月末选Top-N等权组合、持有到下月末"的可交易结果，接入涨跌停/停牌约束和
A股交易成本模型，产出扣费前/扣费后两套业绩指标。

方法论边界（如实记录，不夸大严谨度）：
  - 只用 walk-forward 的样本外预测分数（不用purged K-fold，因为K-fold的训练折
    会包含测试折"未来"的数据，不能代表真实可部署的样本外表现；backtest必须
    只基于"当时能看到的模型"）。
  - 可成交性过滤：入场日（本次rebalance）当天停牌或涨停封死（收盘价>=涨停价，
    参照官方 limit_price 表）的股票不纳入组合——这两种情况现实中大概率买不到。
    出场日（下次rebalance）是否停牌/跌停封死本引擎按市值重估处理（mark-to-market，
    不做逐笔成交模拟）：这是因子/组合回测的标准简化，不是执行算法回测，
    如实记录在此，避免读者误认为已做到逐笔撮合级别的真实性。
  - 成本：research.a_share_rules 的佣金+印花税+过户费+单向滑点，按等权卖出+买入
    的完整往返成本近似扣减（组合每期全部换仓，不做部分持仓延续的增量换手计算）。
"""
from __future__ import annotations

import logging

import numpy as np
import pandas as pd

from research.a_share_rules import CostAssumptions, round_trip_cost_rate

logger = logging.getLogger("research.backtest")


def _load_tradability(conn, symbols: list[str], dates: list) -> pd.DataFrame:
    placeholders = ",".join(["?"] * len(symbols))
    date_list = [pd.Timestamp(d).strftime("%Y-%m-%d") for d in dates]
    date_placeholders = ",".join(["?"] * len(date_list))
    df = conn.execute(
        f"""
        SELECT lp.symbol, lp.trade_date, lp.up_limit, lp.down_limit,
               dq.close,
               (sc.trade_date IS NOT NULL) AS is_suspended
        FROM limit_price lp
        LEFT JOIN daily_quotes dq
            ON dq.symbol = lp.symbol AND dq.trade_date = lp.trade_date AND dq.adjust = 'qfq'
        LEFT JOIN suspend_calendar sc
            ON sc.symbol = lp.symbol AND sc.trade_date = lp.trade_date AND sc.suspend_type = 'S'
        WHERE lp.symbol IN ({placeholders}) AND lp.trade_date IN ({date_placeholders})
        """,
        symbols + date_list,
    ).df()
    df["trade_date"] = pd.to_datetime(df["trade_date"]).dt.date
    df["is_limit_up_locked"] = df["close"] >= df["up_limit"] * 0.998
    df["is_tradable"] = ~(df["is_suspended"].fillna(False) | df["is_limit_up_locked"].fillna(False))
    return df[["symbol", "trade_date", "is_tradable"]]


def run_portfolio_backtest(
    oos_panel: pd.DataFrame,
    top_n: int = 30,
    cost_assumptions: CostAssumptions | None = None,
) -> pd.DataFrame:
    """oos_panel 需含列: symbol, trade_date, pred_score, fwd_return, exchange。
    每个 trade_date 按 pred_score 选 top_n 只可交易股票，等权持有到下期。"""
    from common.db import get_connection

    cost_assumptions = cost_assumptions or CostAssumptions()
    dates = sorted(oos_panel["trade_date"].unique())
    symbols = oos_panel["symbol"].unique().tolist()

    conn = get_connection()
    try:
        tradability = _load_tradability(conn, symbols, dates)
    finally:
        conn.close()

    df = oos_panel.merge(tradability, on=["symbol", "trade_date"], how="left")
    df["is_tradable"] = df["is_tradable"].fillna(True)  # limit_price表缺失（如早期历史）时不因此排除

    rows = []
    for date, grp in df.groupby("trade_date"):
        candidates = grp[grp["is_tradable"] & grp["pred_score"].notna() & grp["fwd_return"].notna()]
        if candidates.empty:
            continue
        selected = candidates.nlargest(top_n, "pred_score")
        gross_return = selected["fwd_return"].mean()
        avg_cost = selected["exchange"].map(lambda ex: round_trip_cost_rate(ex, cost_assumptions)).mean()
        avg_cost += cost_assumptions.slippage_rate  # 买卖各一次滑点，单向假设已在CostAssumptions里定义为往返量级
        net_return = gross_return - avg_cost
        rows.append(
            {
                "trade_date": date,
                "n_selected": len(selected),
                "gross_return": gross_return,
                "net_return": net_return,
                "avg_cost": avg_cost,
            }
        )
    return pd.DataFrame(rows).set_index("trade_date")


def summarize_performance(returns: pd.DataFrame, periods_per_year: int = 12) -> dict:
    def _stats(col: str) -> dict:
        r = returns[col].dropna()
        if r.empty:
            return {}
        cum = (1 + r).cumprod()
        n_years = len(r) / periods_per_year
        cagr = cum.iloc[-1] ** (1 / n_years) - 1 if n_years > 0 else np.nan
        vol_annual = r.std() * np.sqrt(periods_per_year)
        sharpe = (r.mean() * periods_per_year) / vol_annual if vol_annual else np.nan
        drawdown = (cum / cum.cummax() - 1).min()
        return {
            "n_periods": len(r),
            "cumulative_return": round(cum.iloc[-1] - 1, 4),
            "cagr": round(cagr, 4) if pd.notna(cagr) else None,
            "annual_vol": round(vol_annual, 4),
            "sharpe": round(sharpe, 3) if pd.notna(sharpe) else None,
            "max_drawdown": round(drawdown, 4),
            "win_rate": round((r > 0).mean(), 3),
        }

    return {"gross": _stats("gross_return"), "net": _stats("net_return")}


if __name__ == "__main__":
    import json

    from research.factor_eval import add_forward_return
    from research.train_lightgbm import walk_forward_oos_scores

    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    panel_path = "data/feature_panel.parquet"
    panel = pd.read_parquet(panel_path)
    panel = add_forward_return(panel)
    oos = walk_forward_oos_scores(panel)
    logger.info("样本外预测覆盖 %d 行，%d 个截面日", len(oos), oos["trade_date"].nunique())

    bt = run_portfolio_backtest(oos, top_n=30)
    perf = summarize_performance(bt)

    # 全样本外可交易股票等权平均收益作为朴素基准，用于判断多空信号是不是只是
    # "赶上了这几年小盘股普涨"（A股小微盘长期存在超额收益是有文献支持的已知现象，
    # 不加基准对比会让本模型的效果被系统性高估）。
    bench = oos.groupby("trade_date")["fwd_return"].mean().rename("bench_return")
    bt_with_bench = bt.join(bench)
    excess = bt_with_bench["net_return"] - bt_with_bench["bench_return"]
    perf["benchmark_equal_weight_universe"] = {
        "cagr": round((1 + bench).prod() ** (12 / len(bench)) - 1, 4),
        "excess_return_mean_monthly": round(excess.mean(), 4),
        "excess_return_win_rate": round((excess > 0).mean(), 3),
    }

    print(json.dumps(perf, ensure_ascii=False, indent=2))
    bt_with_bench.to_csv("docs/backtest_returns.csv")
