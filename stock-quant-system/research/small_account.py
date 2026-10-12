"""小账户（万元级）可投资域与仓位规则的研究用纯函数。

对照研究：`docs/research-small-account-universe-20261012.md`。
理论：`docs/04-风险管理/10-以损定仓与A股手数.md`（以损定仓 + 100 股取整）、
`docs/01-基础概念/09-A股费用与可成交性.md`（5 元最低佣金对小单的损耗）。
费用一律走 `research.a_share_rules.apply_costs`，不另写税率。

本模块不摸库（panel.py 是 research 里唯一摸库模块），不读 config；生产端的
以损定仓是 `advice/position_sizing.compute_open_size`，这里的 `risk_rule_shares`
是它的参数化副本，`scripts/test_small_account.py` 用真实 compute_open_size 校验两者一致。
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

import numpy as np
import pandas as pd
from scipy import stats

from research.a_share_rules import CostAssumptions, apply_costs

LOT = 100


def floor_lots(shares: float) -> int:
    if shares < LOT:
        return 0
    return int(math.floor(shares / LOT) * LOT)


def ticket_round_trip_cost_rate(
    ticket_cny: float,
    exchange: str,
    cost: CostAssumptions | None = None,
    *,
    include_slippage: bool = True,
) -> float:
    """按单笔金额算往返费率（含 5 元最低佣金）。

    与 `research/backtest.py` 口径一致：买+卖两次 `apply_costs`，滑点 `slippage_rate`
    按往返量级只加一次。ticket 越小，最低佣金占比越高。
    """
    c = cost or CostAssumptions()
    if ticket_cny <= 0:
        return float("nan")
    fees = apply_costs(ticket_cny, "buy", exchange, c) + apply_costs(ticket_cny, "sell", exchange, c)
    rate = fees / ticket_cny
    if include_slippage:
        rate += c.slippage_rate
    return float(rate)


def min_ticket_for_commission_share(max_share: float, cost: CostAssumptions | None = None) -> float:
    """要让单边最低佣金 ≤ 成交额的 max_share，单笔至少多少钱。例：0.003 → 1,666.7 元。"""
    c = cost or CostAssumptions()
    return c.min_commission / max_share


def affordable_shares(price: float, budget_cny: float, exchange: str, cost: CostAssumptions | None = None) -> int:
    """预算内（含买入费用）最多买几股，100 股取整。"""
    if price <= 0 or budget_cny <= 0:
        return 0
    for lots in range(int(budget_cny // (price * LOT)), 0, -1):
        sh = lots * LOT
        if price * sh + apply_costs(price * sh, "buy", exchange, cost) <= budget_cny + 1e-6:
            return sh
    return 0


def risk_rule_shares(
    equity_cny: float,
    cash_cny: float,
    price: float,
    exchange: str,
    *,
    stop_pct: float = 0.08,
    risk_fraction: float = 0.01,
    max_single_weight: float = 0.15,
    cost: CostAssumptions | None = None,
) -> int:
    """现行以损定仓规则的参数化副本：min(风险预算, 单票上限, 可用现金) 再取整到 100 股。"""
    if equity_cny <= 0 or price <= 0 or stop_pct <= 0:
        return 0
    by_risk = floor_lots(risk_fraction * equity_cny / (price * stop_pct))
    by_weight = floor_lots(max_single_weight * equity_cny / price)
    by_cash = affordable_shares(price, cash_cny, exchange, cost)
    return min(by_risk, by_weight, by_cash)


def fixed_slot_shares(
    equity_cny: float,
    cash_cny: float,
    price: float,
    exchange: str,
    *,
    slot_weight: float,
    cost: CostAssumptions | None = None,
) -> int:
    """固定 N 槽位：每槽 slot_weight×净值，100 股取整，受现金约束。"""
    if equity_cny <= 0 or price <= 0:
        return 0
    budget = min(slot_weight * equity_cny, cash_cny)
    return affordable_shares(price, budget, exchange, cost)


def partial_spearman(x: np.ndarray, y: np.ndarray, z: np.ndarray) -> float:
    """把 x、y 分别对 [1, z] 做 OLS 取残差，再算残差间的 Spearman。用于控制价格水平后的 RankIC。"""
    x = np.asarray(x, dtype=float)
    y = np.asarray(y, dtype=float)
    z = np.asarray(z, dtype=float)
    mask = np.isfinite(x) & np.isfinite(y) & np.isfinite(z)
    if mask.sum() < 30:
        return float("nan")
    x, y, z = x[mask], y[mask], z[mask]
    design = np.column_stack([np.ones_like(z), z])
    bx, *_ = np.linalg.lstsq(design, x, rcond=None)
    by, *_ = np.linalg.lstsq(design, y, rcond=None)
    rx = x - design @ bx
    ry = y - design @ by
    rho, _ = stats.spearmanr(rx, ry)
    return float(rho)


def paired_summary(a: pd.Series, b: pd.Series) -> dict:
    """b − a 的配对 t 检验（按期对齐）。返回 n、均值、中位数、t、p。"""
    joined = pd.concat([a, b], axis=1, join="inner").dropna()
    if len(joined) < 3:
        return {"n": int(len(joined))}
    diff = joined.iloc[:, 1] - joined.iloc[:, 0]
    t_stat, p_value = stats.ttest_rel(joined.iloc[:, 1], joined.iloc[:, 0])
    return {
        "n": int(len(joined)),
        "mean_diff": float(diff.mean()),
        "median_diff": float(diff.median()),
        "t_stat": float(t_stat),
        "p_value": float(p_value),
    }


# ---------------------------------------------------------------------------
# 万元账户月度再平衡模拟（整手、止损、最低佣金、滑点）
# ---------------------------------------------------------------------------


@dataclass
class PeriodCandidates:
    """一个再平衡期的候选池（已按模型分数从高到低排好序）。

    price: 入场日未复权收盘价（算手数用）；
    path: 持有期内逐日 close/close_0 比值（qfq，可含分红），长度 = 持有交易日数。
    """

    trade_date: object
    symbols: list[str]
    exchanges: list[str]
    prices: list[float]
    paths: list[np.ndarray]


@dataclass
class SimResult:
    nav_daily: pd.Series
    period_log: pd.DataFrame
    trades: pd.DataFrame
    summary: dict = field(default_factory=dict)


def _sell(shares: int, px: float, exchange: str, c: CostAssumptions) -> tuple[float, float]:
    """返回 (到手现金, 费用+滑点成本)。滑点按 slippage_rate/2 单边，往返合计 = slippage_rate。"""
    fill = px * (1 - c.slippage_rate / 2)
    notional = fill * shares
    fee = apply_costs(notional, "sell", exchange, c)
    slip_cost = (px - fill) * shares
    return notional - fee, fee + slip_cost


def _buy(shares: int, px: float, exchange: str, c: CostAssumptions) -> tuple[float, float]:
    fill = px * (1 + c.slippage_rate / 2)
    notional = fill * shares
    fee = apply_costs(notional, "buy", exchange, c)
    slip_cost = (fill - px) * shares
    return notional + fee, fee + slip_cost


def simulate_small_account(
    periods: list[PeriodCandidates],
    sizing_fn,
    *,
    nav0: float = 10_000.0,
    max_positions: int = 3,
    cash_buffer: float = 0.10,
    stop_loss_pct: float | None = 0.08,
    cost: CostAssumptions | None = None,
) -> SimResult:
    """每期开始全部为现金 → 按序开仓 → 逐日盯市、触发止损即按当日收盘卖出 → 期末清仓。

    sizing_fn(equity, cash, price, exchange) -> shares（已 100 股取整，0 表示买不起）。
    这是组合层回测的简化（不做部分持仓延续、止损按收盘价成交），不是逐笔撮合。
    """
    c = cost or CostAssumptions()
    cash = float(nav0)
    nav_points: list[tuple[object, float]] = []
    period_rows = []
    trade_rows = []

    for per in periods:
        equity = cash
        usable = cash * (1 - cash_buffer)
        positions = []  # dict(symbol, exchange, shares, px0, path)
        fees_period = 0.0
        buy_notional = 0.0
        for sym, ex, px, path in zip(per.symbols, per.exchanges, per.prices, per.paths):
            if len(positions) >= max_positions:
                break
            if not (px > 0) or path is None or len(path) == 0:
                continue
            shares = int(sizing_fn(equity, usable, px, ex))
            if shares < LOT:
                continue
            spend, cost_i = _buy(shares, px, ex, c)
            if spend > usable + 1e-6:
                continue
            usable -= spend
            cash -= spend
            fees_period += cost_i
            buy_notional += px * shares
            positions.append({"symbol": sym, "exchange": ex, "shares": shares, "px0": px, "path": np.asarray(path, float)})
            trade_rows.append({"trade_date": per.trade_date, "symbol": sym, "side": "buy", "shares": shares, "price": px, "cost": cost_i})

        n_open = len(positions)
        horizon = max((len(p["path"]) for p in positions), default=1)
        stops = 0
        for t in range(horizon):
            still = []
            for p in positions:
                ratio = p["path"][min(t, len(p["path"]) - 1)]
                px_t = p["px0"] * ratio
                last_day = t >= len(p["path"]) - 1
                hit_stop = stop_loss_pct is not None and ratio <= 1 - stop_loss_pct
                if hit_stop or last_day:
                    proceeds, cost_i = _sell(p["shares"], px_t, p["exchange"], c)
                    cash += proceeds
                    fees_period += cost_i
                    stops += int(hit_stop and not last_day)
                    trade_rows.append({"trade_date": per.trade_date, "symbol": p["symbol"], "side": "stop" if hit_stop and not last_day else "sell", "shares": p["shares"], "price": px_t, "cost": cost_i, "day": t + 1})
                else:
                    still.append(p)
            positions = still
            mv = sum(p["shares"] * p["px0"] * p["path"][min(t, len(p["path"]) - 1)] for p in positions)
            nav_points.append((per.trade_date, t + 1, cash + mv))
        period_rows.append({
            "trade_date": per.trade_date,
            "nav_start": equity,
            "nav_end": cash,
            "ret": cash / equity - 1 if equity > 0 else np.nan,
            "n_open": n_open,
            "n_stops": stops,
            "fees_and_slippage": fees_period,
            "buy_notional": buy_notional,
            "invested_pct": buy_notional / equity if equity > 0 else np.nan,
        })

    nav_daily = pd.Series(
        [v for _, _, v in nav_points],
        index=pd.MultiIndex.from_tuples([(d, t) for d, t, _ in nav_points], names=["trade_date", "day"]),
        name="nav",
    )
    log = pd.DataFrame(period_rows)
    trades = pd.DataFrame(trade_rows)
    return SimResult(nav_daily, log, trades, summarize_sim(nav_daily, log, nav0))


def summarize_sim(nav_daily: pd.Series, log: pd.DataFrame, nav0: float, periods_per_year: int = 12) -> dict:
    if log.empty:
        return {}
    n_years = len(log) / periods_per_year
    nav_end = float(log["nav_end"].iloc[-1])
    cagr = (nav_end / nav0) ** (1 / n_years) - 1 if n_years > 0 and nav_end > 0 else float("nan")
    path = pd.concat([pd.Series([nav0]), nav_daily.reset_index(drop=True)], ignore_index=True)
    dd = (path / path.cummax() - 1).min()
    rets = log["ret"].astype(float)
    avg_nav = float(log["nav_start"].mean())
    return {
        "n_periods": int(len(log)),
        "nav_end": round(nav_end, 2),
        "cagr": round(float(cagr), 4),
        "max_drawdown_daily": round(float(dd), 4),
        "monthly_ret_mean": round(float(rets.mean()), 4),
        "monthly_ret_std": round(float(rets.std()), 4),
        "sharpe": round(float(rets.mean() / rets.std() * math.sqrt(periods_per_year)), 3) if rets.std() > 0 else float("nan"),
        "win_rate": round(float((rets > 0).mean()), 3),
        "avg_positions": round(float(log["n_open"].mean()), 2),
        "pct_periods_zero_positions": round(float((log["n_open"] == 0).mean()), 3),
        "avg_invested_pct": round(float(log["invested_pct"].mean()), 3),
        "turnover_per_year": round(float(log["buy_notional"].sum() / avg_nav / n_years), 2),
        "cost_drag_pct_per_year": round(float(log["fees_and_slippage"].sum() / avg_nav / n_years), 4),
        "stops_total": int(log["n_stops"].sum()),
    }
