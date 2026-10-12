"""高低点 + KDJ 摆动规则：指标、信号、单标的成交配对。

不读库。口径在跑数之前固定，见模块底部 PROTOCOL。

KDJ 用通达信普通版，不用 TA-Lib STOCH。出处：
https://help.tdx.com.cn/gspt/docs/markdown/redword/IndicatorQA.html
SMA(X,N,M): Y = (X*M + Y'*(N-M)) / N
缺省 N=9, M1=3, M2=3。最高=最低时 RSV 为 NaN，平滑状态保留上一值，当天不发信号。
"""
from __future__ import annotations

from collections import defaultdict

import numpy as np
import pandas as pd

from research.a_share_rules import CostAssumptions, apply_costs, round_trip_cost_rate

# 通达信公式编辑器缺省。本次验证不搜索其他参数。
KDJ_N = 9
KDJ_M1 = 3
KDJ_M2 = 3

# 与 advice/entry_rules.py 开盘封板容差一致。
LIMIT_TOLERANCE = 0.002

# 日历切分在出结果前写死，用来看近年和早期是否同一方向，不是最优断点。
HOLDOUT_CUT = "2023-01-01"

PROTOCOL = {
    "kdj": "通达信 SMA，N=9，M1=3，M2=3；RSV 分母为 0 时不填 50",
    "bars": "前复权 high/low/close 算信号；未复权开盘价对 limit_price 判断能否成交",
    "execution": "t 日收盘出信号，t+1 开盘成交；开盘涨停或停牌则该次买入作废，不追",
    "exit": "开盘跌停或停牌则卖出顺延到之后第一个可卖开盘；样本结束仍持仓的，按最后收盘价记一笔强平",
    "position": "每只股票最多一笔持仓，不加仓",
    "primary": "持仓至少过夜（hold_nights>=1）的全部开仓，含期末强平。避免卖出条件只在顺利时触发",
    "secondary": "仅策略卖出条件在开盘成交的交易",
    "costs_primary": "买卖各计一次单向滑点，再减佣金+过户费+卖出印花税的费率（不含最低 5 元佣金）",
    "costs_retail": "同样滑点，费用改按 100 股调用 apply_costs（含最低佣金）。除权送转会使股数变化，这笔是近似",
    "benchmark": "沪深300：信号平仓对齐开盘价，强平对齐收盘价",
    "holdout_cut": HOLDOUT_CUT,
    "no_parameter_search": True,
    "not_production": "不替换冠军模型，不接入建议引擎",
}


def tdx_sma(values: np.ndarray, n: int, m: int = 1) -> np.ndarray:
    """通达信 SMA。NaN 不重置内部状态，但该根输出为 NaN。"""
    if n <= 0 or m <= 0 or m > n:
        raise ValueError("通达信 SMA 要求 N>=M>=1")
    return tdx_sma_grouped(values, np.zeros(len(values), dtype=np.int64), n, m)


def tdx_sma_grouped(values: np.ndarray, codes: np.ndarray, n: int, m: int = 1) -> np.ndarray:
    """按 codes 分段的通达信 SMA。codes 变化时状态清零。"""
    if n <= 0 or m <= 0 or m > n:
        raise ValueError("通达信 SMA 要求 N>=M>=1")
    x = np.asarray(values, dtype=np.float64)
    codes = np.asarray(codes)
    out = np.full(x.shape[0], np.nan, dtype=np.float64)
    prev = np.nan
    prev_code = None
    cx = m / n
    cy = (n - m) / n
    for i in range(x.shape[0]):
        code = codes[i]
        if code != prev_code:
            prev = np.nan
            prev_code = code
        v = x[i]
        if not np.isfinite(v):
            continue
        prev = v if not np.isfinite(prev) else cx * v + cy * prev
        out[i] = prev
    return out


def _rolling_extrema(values: np.ndarray, codes: np.ndarray, window: int) -> tuple[np.ndarray, np.ndarray]:
    """分组滚动最高、最低。窗口不足 window 的位置为 NaN。"""
    values = np.asarray(values, dtype=np.float64)
    codes = np.asarray(codes)
    hhv = np.full(values.shape[0], np.nan, dtype=np.float64)
    llv = np.full(values.shape[0], np.nan, dtype=np.float64)
    if values.size == 0:
        return hhv, llv
    breaks = np.flatnonzero(np.diff(codes)) + 1
    starts = np.r_[0, breaks]
    ends = np.r_[breaks, values.shape[0]]
    for start, end in zip(starts, ends):
        series = pd.Series(values[start:end])
        hhv[start:end] = series.rolling(window, min_periods=window).max().to_numpy()
        llv[start:end] = series.rolling(window, min_periods=window).min().to_numpy()
    return hhv, llv


def compute_kdj_frame(
    df: pd.DataFrame,
    n: int = KDJ_N,
    m1: int = KDJ_M1,
    m2: int = KDJ_M2,
) -> pd.DataFrame:
    """要求含 symbol/high/low/close，且已按 symbol、日期排序。返回副本并附上 kdj_k/d/j。"""
    out = df.copy()
    codes, _ = pd.factorize(out["symbol"], sort=False)
    high = out["high"].to_numpy(dtype=np.float64)
    low = out["low"].to_numpy(dtype=np.float64)
    close = out["close"].to_numpy(dtype=np.float64)
    hhv, _ = _rolling_extrema(high, codes, n)
    _, llv = _rolling_extrema(low, codes, n)
    span = hhv - llv
    rsv = np.full(len(out), np.nan, dtype=np.float64)
    ok = np.isfinite(span) & (span > 0) & np.isfinite(close) & np.isfinite(llv)
    rsv[ok] = (close[ok] - llv[ok]) / span[ok] * 100.0
    k = tdx_sma_grouped(rsv, codes, m1, 1)
    d = tdx_sma_grouped(k, codes, m2, 1)
    out["kdj_k"] = k
    out["kdj_d"] = d
    out["kdj_j"] = 3.0 * k - 2.0 * d
    return out


def attach_swing_signals(df: pd.DataFrame) -> pd.DataFrame:
    """父亲的规则，严格不等号。

    买入：低点抬高、高点抬高、K<30、J<5 且 J 高于上日。
    卖出：高点降低、低点降低、且 K>70，同时满足下面任一：
      J>90 且 J 低于上日；或 J 下穿 D（上日 J>=上日 D，今日 J<今日 D）。
    """
    out = df.copy()
    grouped = out.groupby("symbol", sort=False)
    prev_high = grouped["high"].shift(1)
    prev_low = grouped["low"].shift(1)
    prev_j = grouped["kdj_j"].shift(1)
    prev_d = grouped["kdj_d"].shift(1)
    k = out["kdj_k"]
    j = out["kdj_j"]
    d = out["kdj_d"]
    higher = (out["low"] > prev_low) & (out["high"] > prev_high)
    lower = (out["high"] < prev_high) & (out["low"] < prev_low)
    j_turn_up = (j < 5) & (j > prev_j)
    j_turn_down = (j > 90) & (j < prev_j)
    j_cross_down = (prev_j >= prev_d) & (j < d)
    out["buy_signal"] = (higher & (k < 30) & j_turn_up).fillna(False).astype(bool)
    out["sell_signal"] = (lower & (k > 70) & (j_turn_down | j_cross_down)).fillna(False).astype(bool)
    return out


def _fee_split(exchange: str, costs: CostAssumptions) -> tuple[str, float, float]:
    ex = exchange if exchange in ("sh", "sz", "bj") else "sh"
    stamp = 0.0 if ex == "bj" else costs.stamp_tax_rate
    buy_fee = costs.commission_rate + costs.transfer_fee_rate
    sell_fee = costs.commission_rate + costs.transfer_fee_rate + stamp
    return ex, buy_fee, sell_fee


def _raw_px(qfq_px: float, qfq_open: float, raw_open: float) -> float:
    if np.isfinite(raw_open) and np.isfinite(qfq_open) and qfq_open > 0 and np.isfinite(qfq_px):
        return float(qfq_px * raw_open / qfq_open)
    return np.nan


def _retail_fee_rate(
    raw_entry: float,
    raw_exit: float,
    exchange: str,
    costs: CostAssumptions,
    shares: int = 100,
) -> float:
    if not np.isfinite(raw_entry) or not np.isfinite(raw_exit) or raw_entry <= 0 or raw_exit <= 0:
        return np.nan
    notional_buy = raw_entry * shares
    notional_sell = raw_exit * shares
    fee = apply_costs(notional_buy, "buy", exchange, costs) + apply_costs(
        notional_sell, "sell", exchange, costs
    )
    return float(fee / notional_buy)


class BookAccum:
    """等权账户：同一天有多只持仓时，日收益取算术平均。"""

    def __init__(self) -> None:
        self.sum_ret: dict = defaultdict(float)
        self.count: dict = defaultdict(int)

    def add(self, day, ret: float) -> None:
        if ret is None or not np.isfinite(ret):
            return
        key = pd.Timestamp(day).date()
        self.sum_ret[key] += float(ret)
        self.count[key] += 1


def position_day_returns(
    dates: np.ndarray,
    open_: np.ndarray,
    close: np.ndarray,
    entry_i: int,
    exit_i: int,
    *,
    slip: float,
    buy_fee: float,
    sell_fee: float,
    exit_on_close: bool,
) -> list[tuple]:
    """持仓路径上的简单日收益。滑点打在成交价上，费率在进/出当日扣。"""
    buy_px = float(open_[entry_i]) * (1.0 + slip)
    if buy_px <= 0 or not np.isfinite(buy_px):
        return []
    if exit_i == entry_i:
        sell_px = float(close[entry_i]) * (1.0 - slip)
        if sell_px <= 0 or not np.isfinite(sell_px):
            return []
        return [(dates[entry_i], sell_px / buy_px - 1.0 - buy_fee - sell_fee)]
    rows = [(dates[entry_i], float(close[entry_i]) / buy_px - 1.0 - buy_fee)]
    for i in range(entry_i + 1, exit_i):
        prev = float(close[i - 1])
        if prev <= 0 or not np.isfinite(close[i]):
            return []
        rows.append((dates[i], float(close[i]) / prev - 1.0))
    prev = float(close[exit_i - 1])
    if prev <= 0:
        return []
    exit_raw = float(close[exit_i] if exit_on_close else open_[exit_i])
    sell_px = exit_raw * (1.0 - slip)
    if sell_px <= 0 or not np.isfinite(sell_px):
        return []
    rows.append((dates[exit_i], sell_px / prev - 1.0 - sell_fee))
    return rows


def simulate_symbol_frame(
    frame: pd.DataFrame,
    costs: CostAssumptions | None = None,
    book: BookAccum | None = None,
) -> tuple[list[dict], dict]:
    """单标的、已按日期升序。买卖信号列须已算好。"""
    costs = costs or CostAssumptions()
    g = frame
    n = len(g)
    empty_counts = {
        "n_buy_signal": 0,
        "n_buy_ignored_in_position": 0,
        "n_entry_unfilled": 0,
        "n_entry_missing_limit": 0,
        "n_signal_expired_eod": 0,
    }
    if n == 0:
        return [], empty_counts

    dates = g["trade_date"].to_numpy()
    open_ = g["open"].to_numpy(dtype=np.float64)
    close = g["close"].to_numpy(dtype=np.float64)
    buy = g["buy_signal"].to_numpy(dtype=bool)
    sell = g["sell_signal"].to_numpy(dtype=bool)
    raw_open = g["raw_open"].to_numpy(dtype=np.float64) if "raw_open" in g.columns else np.full(n, np.nan)
    up_limit = g["up_limit"].to_numpy(dtype=np.float64) if "up_limit" in g.columns else np.full(n, np.nan)
    down_limit = g["down_limit"].to_numpy(dtype=np.float64) if "down_limit" in g.columns else np.full(n, np.nan)
    suspended = (
        g["suspended"].fillna(False).to_numpy(dtype=bool)
        if "suspended" in g.columns
        else np.zeros(n, dtype=bool)
    )
    exchange = str(g["exchange"].iloc[0]) if "exchange" in g.columns and pd.notna(g["exchange"].iloc[0]) else ""
    board = str(g["board"].iloc[0]) if "board" in g.columns and pd.notna(g["board"].iloc[0]) else "unknown"
    is_st = bool(g["is_st"].iloc[0]) if "is_st" in g.columns and pd.notna(g["is_st"].iloc[0]) else False
    symbol = g["symbol"].iloc[0]
    ex, buy_fee, sell_fee = _fee_split(exchange, costs)
    slip = float(costs.slippage_rate)
    fee_rate = round_trip_cost_rate(ex, costs)

    def can_buy(i: int) -> bool:
        if suspended[i] or not np.isfinite(open_[i]) or open_[i] <= 0:
            return False
        up = up_limit[i]
        px = raw_open[i] if np.isfinite(raw_open[i]) else open_[i]
        if np.isfinite(up) and np.isfinite(px) and px >= up * (1.0 - LIMIT_TOLERANCE):
            return False
        return True

    def can_sell(i: int) -> bool:
        if suspended[i] or not np.isfinite(open_[i]) or open_[i] <= 0:
            return False
        down = down_limit[i]
        px = raw_open[i] if np.isfinite(raw_open[i]) else open_[i]
        if np.isfinite(down) and np.isfinite(px) and px <= down * (1.0 + LIMIT_TOLERANCE):
            return False
        return True

    def pack(entry_i: int, exit_i: int, reason: str) -> dict:
        entry_open = float(open_[entry_i])
        exit_on_close = reason != "signal"
        exit_px = float(close[exit_i] if exit_on_close else open_[exit_i])
        buy_px = entry_open * (1.0 + slip)
        sell_px = exit_px * (1.0 - slip)
        gross = exit_px / entry_open - 1.0
        gross_slip = sell_px / buy_px - 1.0
        raw_entry = float(raw_open[entry_i]) if np.isfinite(raw_open[entry_i]) else np.nan
        if exit_on_close:
            raw_exit = _raw_px(exit_px, float(open_[exit_i]), float(raw_open[exit_i]))
        else:
            raw_exit = float(raw_open[exit_i]) if np.isfinite(raw_open[exit_i]) else np.nan
        retail_fee = _retail_fee_rate(raw_entry, raw_exit, ex, costs)
        limit_checked = bool(np.isfinite(up_limit[entry_i]))
        return {
            "symbol": symbol,
            "exchange": ex,
            "board": board,
            "is_st": is_st,
            "entry_date": pd.Timestamp(dates[entry_i]),
            "exit_date": pd.Timestamp(dates[exit_i]),
            "hold_nights": int(exit_i - entry_i),
            "exit_reason": reason,
            "gross_return": gross,
            "net_return": gross_slip - fee_rate,
            "net_return_retail": (gross_slip - retail_fee) if np.isfinite(retail_fee) else np.nan,
            "entry_limit_checked": limit_checked,
            "exit_blocked_on_mark": bool(reason != "signal" and not can_sell(exit_i)),
        }

    pending = None
    entry_i = -1
    n_ignored = 0
    n_unfilled = 0
    n_missing_limit = 0
    trades: list[dict] = []

    for i in range(n):
        if pending == "buy" and entry_i < 0:
            if can_buy(i):
                entry_i = i
                if not np.isfinite(up_limit[i]):
                    n_missing_limit += 1
            else:
                n_unfilled += 1
            pending = None
        elif pending == "sell" and entry_i >= 0:
            if can_sell(i):
                trade = pack(entry_i, i, "signal")
                trades.append(trade)
                if book is not None and trade["hold_nights"] >= 1:
                    for day, ret in position_day_returns(
                        dates, open_, close, entry_i, i,
                        slip=slip, buy_fee=buy_fee, sell_fee=sell_fee, exit_on_close=False,
                    ):
                        book.add(day, ret)
                entry_i = -1
                pending = None

        if entry_i < 0 and pending is None and buy[i]:
            pending = "buy"
        elif entry_i >= 0 and pending != "sell" and sell[i]:
            pending = "sell"
        elif entry_i >= 0 and buy[i]:
            n_ignored += 1

    if entry_i >= 0:
        trade = pack(entry_i, n - 1, "forced_eod")
        trades.append(trade)
        if book is not None and trade["hold_nights"] >= 1:
            for day, ret in position_day_returns(
                dates, open_, close, entry_i, n - 1,
                slip=slip, buy_fee=buy_fee, sell_fee=sell_fee, exit_on_close=True,
            ):
                book.add(day, ret)

    counts = {
        "n_buy_signal": int(buy.sum()),
        "n_buy_ignored_in_position": n_ignored,
        "n_entry_unfilled": n_unfilled,
        "n_entry_missing_limit": n_missing_limit,
        "n_signal_expired_eod": int(pending == "buy"),
    }
    return trades, counts


def _dist_stats(series: pd.Series) -> dict:
    r = series.dropna().astype(float)
    if r.empty:
        return {"n": 0}
    wins = r[r > 0]
    losses = r[r <= 0]
    profit_factor = None
    if len(losses) and float(losses.sum()) != 0:
        profit_factor = float(wins.sum() / abs(losses.sum()))
    out = {
        "n": int(len(r)),
        "win_rate": round(float((r > 0).mean()), 4),
        "mean": round(float(r.mean()), 6),
        "median": round(float(r.median()), 6),
        "std": round(float(r.std(ddof=1)), 6) if len(r) > 1 else None,
        "p05": round(float(r.quantile(0.05)), 6),
        "p95": round(float(r.quantile(0.95)), 6),
        "avg_win": round(float(wins.mean()), 6) if len(wins) else None,
        "avg_loss": round(float(losses.mean()), 6) if len(losses) else None,
        "profit_factor": round(profit_factor, 4) if profit_factor is not None else None,
        "sum": round(float(r.sum()), 4),
    }
    out["t_pvalue"] = None
    if len(r) > 1 and float(r.std(ddof=1)) > 0:
        try:
            from scipy import stats

            test = stats.ttest_1samp(r.to_numpy(), 0.0)
            out["t_stat"] = round(float(test.statistic), 3)
            out["t_pvalue"] = float(test.pvalue)
        except Exception:
            out["t_pvalue"] = None
    return out


def _group_table(df: pd.DataFrame, key: str, col: str = "net_return") -> list[dict]:
    rows = []
    if df.empty or key not in df.columns:
        return rows
    for name, part in df.groupby(key, dropna=False):
        stats = _dist_stats(part[col])
        stats["key"] = None if pd.isna(name) else str(name) if not isinstance(name, (int, np.integer)) else int(name)
        rows.append(stats)
    rows.sort(key=lambda item: str(item["key"]))
    return rows


def summarize_trades(trades: pd.DataFrame) -> dict:
    """主口径 hold_nights>=1；对照口径仅 exit_reason==signal。"""
    if trades.empty:
        return {"primary_net": {"n": 0}, "signal_exit_net": {"n": 0}}
    primary = trades[trades["hold_nights"] >= 1].copy()
    signal = trades[trades["exit_reason"] == "signal"].copy()
    primary["entry_year"] = pd.to_datetime(primary["entry_date"]).dt.year
    signal["entry_year"] = pd.to_datetime(signal["entry_date"]).dt.year
    cut = pd.Timestamp(HOLDOUT_CUT)
    summary = {
        "n_rows": int(len(trades)),
        "n_primary": int(len(primary)),
        "n_same_day_mark": int((trades["hold_nights"] < 1).sum()),
        "n_signal_exit": int(len(signal)),
        "n_forced_in_primary": int((primary["exit_reason"] != "signal").sum()) if len(primary) else 0,
        "primary_gross": _dist_stats(primary["gross_return"]),
        "primary_net": _dist_stats(primary["net_return"]),
        "primary_net_retail": _dist_stats(primary["net_return_retail"]),
        "signal_exit_net": _dist_stats(signal["net_return"]),
        "signal_exit_gross": _dist_stats(signal["gross_return"]),
        "hold_nights_primary": {
            "mean": round(float(primary["hold_nights"].mean()), 2) if len(primary) else None,
            "median": round(float(primary["hold_nights"].median()), 2) if len(primary) else None,
        },
        "by_year_primary_net": _group_table(primary, "entry_year"),
        "by_board_primary_net": _group_table(primary, "board"),
        "by_st_primary_net": _group_table(primary.assign(st=primary["is_st"].map({True: "st", False: "not_st"})), "st"),
        "before_cut_primary_net": _dist_stats(primary.loc[pd.to_datetime(primary["entry_date"]) < cut, "net_return"]),
        "after_cut_primary_net": _dist_stats(primary.loc[pd.to_datetime(primary["entry_date"]) >= cut, "net_return"]),
        "t_test_note": "交易在时间和股票上相关。t 检验把它们当成独立样本，p 值偏乐观，不能当成策略显著的证据。",
        "st_flag_note": "is_st 是股票池里的当前标记，不是历史上每一天是否 ST。",
    }
    if "excess_net" in primary.columns:
        summary["primary_excess_net"] = _dist_stats(primary["excess_net"])
    if "excess_gross" in primary.columns:
        summary["primary_excess_gross"] = _dist_stats(primary["excess_gross"])
        valid = primary["excess_gross"].dropna()
        summary["primary_beat_index_rate"] = round(float((valid > 0).mean()), 4) if len(valid) else None
    return summary


def summarize_book(book: BookAccum, index_close: pd.Series) -> dict:
    """现金日记 0。index_close 的索引是 datetime.date。"""
    if not book.count:
        return {"n_days": 0}
    start = min(book.count)
    end = max(book.count)
    cal = sorted(d for d in index_close.index if start <= d <= end)
    extra = [d for d in book.count if d not in set(cal) and start <= d <= end]
    days = sorted(set(cal) | set(extra))
    if not days:
        return {"n_days": 0}
    rets = []
    counts = []
    idx_rets = []
    prev_idx = None
    for day in days:
        n_pos = book.count.get(day, 0)
        counts.append(n_pos)
        rets.append(book.sum_ret[day] / n_pos if n_pos else 0.0)
        px = index_close.get(day, np.nan)
        if prev_idx is not None and np.isfinite(prev_idx) and np.isfinite(px) and prev_idx > 0:
            idx_rets.append(float(px) / float(prev_idx) - 1.0)
        else:
            idx_rets.append(0.0)
        if np.isfinite(px):
            prev_idx = px
    r = pd.Series(rets, index=days, dtype=float)
    b = pd.Series(idx_rets, index=days, dtype=float)
    eq = (1.0 + r).cumprod()
    bench = (1.0 + b).cumprod()
    n_years = len(r) / 252.0
    vol = float(r.std(ddof=1)) if len(r) > 1 else np.nan
    sharpe = (float(r.mean()) * 252.0 / (vol * np.sqrt(252.0))) if vol and np.isfinite(vol) and vol > 0 else None
    cagr = float(eq.iloc[-1] ** (1.0 / n_years) - 1.0) if n_years > 0 and eq.iloc[-1] > 0 else None
    bench_cagr = float(bench.iloc[-1] ** (1.0 / n_years) - 1.0) if n_years > 0 and bench.iloc[-1] > 0 else None
    dd = float((eq / eq.cummax() - 1.0).min())
    invested = [c for c in counts if c > 0]
    return {
        "n_days": int(len(r)),
        "time_in_market": round(float(np.mean([c > 0 for c in counts])), 4),
        "avg_positions_invested_days": round(float(np.mean(invested)), 2) if invested else 0,
        "max_positions": int(max(counts) if counts else 0),
        "cagr": round(cagr, 4) if cagr is not None else None,
        "benchmark_cagr": round(bench_cagr, 4) if bench_cagr is not None else None,
        "annual_vol": round(float(vol * np.sqrt(252.0)), 4) if np.isfinite(vol) else None,
        "sharpe_rf0": round(float(sharpe), 3) if sharpe is not None else None,
        "max_drawdown": round(dd, 4),
        "cumulative_return": round(float(eq.iloc[-1] - 1.0), 4),
        "benchmark_cumulative_return": round(float(bench.iloc[-1] - 1.0), 4),
        "note": "空仓日收益按 0。Sharpe 无风险利率为 0，与 research/backtest.py 的习惯一致，但是日频。费用按费率扣在进出当日，和逐笔净收益的减法不是同一条复利路径。",
    }
