"""research/small_account.py 合成数据测试（不摸库）。运行：python -m scripts.test_small_account"""
from __future__ import annotations

import math
import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from advice.position_sizing import compute_open_size
from research.a_share_rules import CostAssumptions, apply_costs
from research.small_account import (
    PeriodCandidates,
    affordable_shares,
    fixed_slot_shares,
    min_ticket_for_commission_share,
    paired_summary,
    partial_spearman,
    risk_rule_shares,
    simulate_small_account,
    ticket_round_trip_cost_rate,
)


def test_ticket_cost_rate_min_commission_dominates_small_ticket():
    c = CostAssumptions()
    r1000 = ticket_round_trip_cost_rate(1000, "sz", c)
    r10000 = ticket_round_trip_cost_rate(10000, "sz", c)
    # 1000 元：佣金 5+5，印花 0.5，过户 0.02，滑点 1.0 → 11.52/1000
    assert math.isclose(r1000, (5 + 5 + 0.5 + 0.01 + 0.01) / 1000 + 0.001, rel_tol=1e-9), r1000
    assert r1000 > r10000 * 3
    # 北交所免印花税
    assert ticket_round_trip_cost_rate(1000, "bj", c) < r1000
    assert math.isclose(min_ticket_for_commission_share(0.003), 5 / 0.003)


def test_affordable_shares_includes_fee():
    # 10 元股，预算 1000：1 手 = 1000 + 5.01 费用 > 1000 → 买不起
    assert affordable_shares(10.0, 1000.0, "sh") == 0
    assert affordable_shares(10.0, 1006.0, "sh") == 100
    assert affordable_shares(10.0, 2600.0, "sh") == 200


def test_risk_rule_matches_production_compute_open_size():
    for price in (3.3, 8.0, 12.4, 12.6, 15.0, 44.92, 500.0):
        prod = compute_open_size(10_000.0, price=price, stop_price=round(price * 0.92, 2), exchange="sh")
        mine = risk_rule_shares(10_000.0, 9_000.0, price, "sh", stop_pct=1 - round(price * 0.92, 2) / price)
        assert prod.shares == mine, (price, prod.shares, mine)


def test_fixed_slot_shares():
    assert fixed_slot_shares(10_000.0, 9_000.0, 30.0, "sz", slot_weight=0.45) == 100
    assert fixed_slot_shares(10_000.0, 9_000.0, 50.0, "sz", slot_weight=0.45) == 0
    assert fixed_slot_shares(10_000.0, 9_000.0, 5.0, "sz", slot_weight=0.30) == 500


def test_partial_spearman_removes_common_price_component():
    rng = np.random.default_rng(0)
    z = rng.normal(size=2000)
    x = 2 * z + rng.normal(size=2000) * 0.1
    y = -3 * z + rng.normal(size=2000) * 0.1
    from scipy import stats

    raw, _ = stats.spearmanr(x, y)
    assert raw < -0.9
    assert abs(partial_spearman(x, y, z)) < 0.1


def test_paired_summary_basic():
    import pandas as pd

    a = pd.Series([0.01, 0.02, 0.03, 0.04], index=[1, 2, 3, 4])
    b = a + pd.Series([0.009, 0.011, 0.010, 0.010], index=a.index)
    out = paired_summary(a, b)
    assert out["n"] == 4 and math.isclose(out["mean_diff"], 0.01) and out["p_value"] < 0.01


def _periods(path_a, path_b, price_a=10.0, price_b=20.0):
    return [
        PeriodCandidates(
            trade_date="2020-01-31",
            symbols=["A", "B"],
            exchanges=["sz", "sh"],
            prices=[price_a, price_b],
            paths=[np.asarray(path_a, float), np.asarray(path_b, float)],
        )
    ]


def test_simulation_stop_loss_and_costs():
    c = CostAssumptions()
    flat = [1.0, 1.0, 1.0]
    crash = [0.95, 0.90, 1.10]  # 第 2 日触发 -8% 止损，之后反弹也不参与
    sizing = lambda eq, cash, px, ex: fixed_slot_shares(eq, cash, px, ex, slot_weight=0.45)
    res = simulate_small_account(_periods(flat, crash), sizing, nav0=10_000.0, max_positions=2, stop_loss_pct=0.08, cost=c)
    assert res.period_log["n_open"].iloc[0] == 2
    assert res.period_log["n_stops"].iloc[0] == 1
    stop_trade = res.trades[res.trades["side"] == "stop"].iloc[0]
    assert stop_trade["day"] == 2 and math.isclose(stop_trade["price"], 18.0)
    # 不止损版本：B 以 1.10 卖出，净值更高
    res_nostop = simulate_small_account(_periods(flat, crash), sizing, nav0=10_000.0, max_positions=2, stop_loss_pct=None, cost=c)
    assert res_nostop.period_log["nav_end"].iloc[0] > res.period_log["nav_end"].iloc[0]
    # 平盘持仓 A（400 股 ×10 元）费用 = 买卖佣金 5+5 + 印花 2 + 过户 0.08 + 滑点 4 ≈ 16.1
    a_trades = res.trades[res.trades["symbol"] == "A"]
    fee_a = a_trades["cost"].sum()
    expect = apply_costs(4000 * (1 + 0.0005), "buy", "sz", c) + apply_costs(4000 * (1 - 0.0005), "sell", "sz", c) + 4000 * 0.001
    assert math.isclose(fee_a, expect, rel_tol=1e-6), (fee_a, expect)
    assert res.summary["n_periods"] == 1 and res.summary["cost_drag_pct_per_year"] > 0


def test_simulation_cannot_afford_lot_leaves_cash():
    sizing = lambda eq, cash, px, ex: risk_rule_shares(eq, cash, px, ex)
    res = simulate_small_account(_periods([1.1, 1.1], [1.1, 1.1], price_a=200.0, price_b=300.0), sizing, nav0=10_000.0)
    assert res.period_log["n_open"].iloc[0] == 0
    assert math.isclose(res.period_log["nav_end"].iloc[0], 10_000.0)
    assert res.summary["pct_periods_zero_positions"] == 1.0


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for t in tests:
        t()
        print("OK", t.__name__)
    print(f"{len(tests)} tests passed")
