"""KDJ 摆动规则：公式、信号边界、次日开盘成交。不连接生产库。"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT))

from research.a_share_rules import CostAssumptions
from research.kdj_swing import (
    attach_swing_signals,
    compute_kdj_frame,
    position_day_returns,
    simulate_symbol_frame,
    tdx_sma,
)


def test_tdx_sma_matches_recurrence() -> None:
    out = tdx_sma(np.array([10.0, 20.0, np.nan, 30.0]), n=3, m=1)
    assert out[0] == 10.0
    assert abs(out[1] - (20.0 + 2.0 * 10.0) / 3.0) < 1e-12
    assert np.isnan(out[2])
    assert abs(out[3] - (30.0 + 2.0 * out[1]) / 3.0) < 1e-12


def test_kdj_hand_values() -> None:
    df = pd.DataFrame(
        {
            "symbol": ["S"] * 5,
            "high": [10.0, 12.0, 11.0, 13.0, 14.0],
            "low": [8.0, 9.0, 7.0, 10.0, 11.0],
            "close": [9.0, 11.0, 8.0, 12.0, 13.0],
        }
    )
    out = compute_kdj_frame(df, n=3, m1=3, m2=3)
    assert np.isnan(out.loc[1, "kdj_k"])
    assert abs(out.loc[2, "kdj_k"] - 20.0) < 1e-9
    assert abs(out.loc[2, "kdj_j"] - 20.0) < 1e-9
    assert abs(out.loc[3, "kdj_k"] - 41.1111111111) < 1e-8
    assert abs(out.loc[3, "kdj_d"] - 27.0370370370) < 1e-8
    assert abs(out.loc[3, "kdj_j"] - 69.2592592593) < 1e-8
    assert abs(out.loc[4, "kdj_k"] - 55.9788359788) < 1e-8
    assert abs(out.loc[4, "kdj_j"] - 94.5679012346) < 1e-8


def test_flat_range_does_not_invent_rsv() -> None:
    df = pd.DataFrame(
        {
            "symbol": ["S"] * 12,
            "high": np.full(12, 10.0),
            "low": np.full(12, 10.0),
            "close": np.full(12, 10.0),
        }
    )
    out = compute_kdj_frame(df, n=9, m1=3, m2=3)
    assert out["kdj_k"].isna().all()


def test_groups_do_not_leak() -> None:
    a = pd.DataFrame(
        {
            "symbol": ["A"] * 12,
            "high": np.linspace(10, 21, 12),
            "low": np.linspace(8, 19, 12),
            "close": np.linspace(9, 20, 12),
        }
    )
    b = pd.DataFrame(
        {
            "symbol": ["B"] * 12,
            "high": np.full(12, 5.0),
            "low": np.full(12, 4.0),
            "close": np.full(12, 4.5),
        }
    )
    both = compute_kdj_frame(pd.concat([a, b], ignore_index=True))
    alone = compute_kdj_frame(b.reset_index(drop=True))
    got = both.loc[both["symbol"] == "B", "kdj_k"].to_numpy()
    exp = alone["kdj_k"].to_numpy()
    assert np.allclose(got, exp, equal_nan=True)
    # 窗口够了之后 RSV=(4.5-4)/(5-4)*100=50，K 保持 50。
    assert abs(got[8] - 50.0) < 1e-9
    assert abs(got[-1] - 50.0) < 1e-9


def _signal_frame(rows: list[dict]) -> pd.DataFrame:
    df = pd.DataFrame(rows)
    df["symbol"] = "S"
    return attach_swing_signals(df)


def test_buy_requires_all_strict_conditions() -> None:
    base = [
        {"high": 10.0, "low": 8.0, "kdj_k": 20.0, "kdj_d": 20.0, "kdj_j": 0.0},
        {"high": 11.0, "low": 9.0, "kdj_k": 29.0, "kdj_d": 15.0, "kdj_j": 3.0},
    ]
    assert bool(_signal_frame(base).loc[1, "buy_signal"])

    k30 = [dict(base[0]), dict(base[1], kdj_k=30.0)]
    assert not bool(_signal_frame(k30).loc[1, "buy_signal"])

    j5 = [dict(base[0]), dict(base[1], kdj_j=5.0)]
    assert not bool(_signal_frame(j5).loc[1, "buy_signal"])

    flat_low = [dict(base[0]), dict(base[1], low=8.0)]
    assert not bool(_signal_frame(flat_low).loc[1, "buy_signal"])


def test_sell_branches() -> None:
    j_fade = _signal_frame(
        [
            {"high": 10.0, "low": 8.0, "kdj_k": 80.0, "kdj_d": 70.0, "kdj_j": 95.0},
            {"high": 9.0, "low": 7.0, "kdj_k": 75.0, "kdj_d": 80.0, "kdj_j": 92.0},
        ]
    )
    assert bool(j_fade.loc[1, "sell_signal"])

    cross = _signal_frame(
        [
            {"high": 10.0, "low": 8.0, "kdj_k": 80.0, "kdj_d": 60.0, "kdj_j": 70.0},
            {"high": 9.0, "low": 7.0, "kdj_k": 75.0, "kdj_d": 65.0, "kdj_j": 55.0},
        ]
    )
    assert bool(cross.loc[1, "sell_signal"])

    k70 = _signal_frame(
        [
            {"high": 10.0, "low": 8.0, "kdj_k": 80.0, "kdj_d": 60.0, "kdj_j": 70.0},
            {"high": 9.0, "low": 7.0, "kdj_k": 70.0, "kdj_d": 65.0, "kdj_j": 55.0},
        ]
    )
    assert not bool(k70.loc[1, "sell_signal"])

    no_cross = _signal_frame(
        [
            {"high": 10.0, "low": 8.0, "kdj_k": 80.0, "kdj_d": 60.0, "kdj_j": 50.0},
            {"high": 9.0, "low": 7.0, "kdj_k": 75.0, "kdj_d": 70.0, "kdj_j": 40.0},
        ]
    )
    assert not bool(no_cross.loc[1, "sell_signal"])


def _exec_frame(n: int = 6) -> pd.DataFrame:
    return pd.DataFrame(
        {
            "symbol": ["X"] * n,
            "trade_date": pd.date_range("2024-01-02", periods=n, freq="B"),
            "open": np.full(n, 10.0),
            "high": np.full(n, 10.5),
            "low": np.full(n, 9.5),
            "close": np.full(n, 10.2),
            "buy_signal": np.zeros(n, dtype=bool),
            "sell_signal": np.zeros(n, dtype=bool),
            "raw_open": np.full(n, 10.0),
            "up_limit": np.full(n, 11.0),
            "down_limit": np.full(n, 9.0),
            "suspended": np.zeros(n, dtype=bool),
            "exchange": ["sh"] * n,
            "board": ["main"] * n,
            "is_st": [False] * n,
        }
    )


def test_fills_next_open_and_costs() -> None:
    df = _exec_frame()
    df.loc[0, "buy_signal"] = True
    df.loc[2, "sell_signal"] = True
    df.loc[1, "open"] = 10.0
    df.loc[1, "raw_open"] = 10.0
    df.loc[3, "open"] = 11.0
    df.loc[3, "raw_open"] = 11.0
    trades, counts = simulate_symbol_frame(df)
    assert counts["n_entry_unfilled"] == 0
    assert len(trades) == 1
    trade = trades[0]
    assert trade["exit_reason"] == "signal"
    assert trade["hold_nights"] == 2
    assert abs(trade["gross_return"] - 0.1) < 1e-12
    buy_px = 10.0 * 1.001
    sell_px = 11.0 * 0.999
    net = sell_px / buy_px - 1.0 - (2 * 0.0003 + 2 * 0.00001 + 0.0005)
    assert abs(trade["net_return"] - net) < 1e-12
    assert abs(trade["net_return_retail"] - (sell_px / buy_px - 1.0 - 10.571 / 1000.0)) < 1e-12


def test_limit_up_open_cancels_entry() -> None:
    df = _exec_frame()
    df.loc[0, "buy_signal"] = True
    df.loc[1, "raw_open"] = 11.0
    df.loc[1, "up_limit"] = 11.0
    trades, counts = simulate_symbol_frame(df)
    assert trades == []
    assert counts["n_entry_unfilled"] == 1


def test_limit_down_defers_exit() -> None:
    df = _exec_frame(5)
    df.loc[0, "buy_signal"] = True
    df.loc[2, "sell_signal"] = True
    df.loc[3, "raw_open"] = 9.0
    df.loc[3, "down_limit"] = 9.0
    df.loc[4, "open"] = 12.0
    df.loc[4, "raw_open"] = 12.0
    trades, _ = simulate_symbol_frame(df)
    assert len(trades) == 1
    assert trades[0]["exit_reason"] == "signal"
    assert pd.Timestamp(trades[0]["exit_date"]) == df.loc[4, "trade_date"]
    assert abs(trades[0]["gross_return"] - 0.2) < 1e-12


def test_open_position_marked_at_last_close() -> None:
    df = _exec_frame(4)
    df.loc[0, "buy_signal"] = True
    df.loc[3, "close"] = 13.0
    trades, _ = simulate_symbol_frame(df)
    assert len(trades) == 1
    assert trades[0]["exit_reason"] == "forced_eod"
    assert trades[0]["hold_nights"] == 2
    assert abs(trades[0]["gross_return"] - (13.0 / 10.0 - 1.0)) < 1e-12


def test_position_day_path_zero_cost() -> None:
    dates = pd.date_range("2024-01-02", periods=2, freq="B").to_numpy()
    rows = position_day_returns(
        dates,
        np.array([10.0, 12.0]),
        np.array([11.0, 12.5]),
        0,
        1,
        slip=0.0,
        buy_fee=0.0,
        sell_fee=0.0,
        exit_on_close=False,
    )
    assert abs(rows[0][1] - 0.1) < 1e-12
    assert abs(rows[1][1] - (12.0 / 11.0 - 1.0)) < 1e-12


def test_zero_costs_object_matches_price_ratio() -> None:
    df = _exec_frame()
    df.loc[0, "buy_signal"] = True
    df.loc[2, "sell_signal"] = True
    df.loc[3, "open"] = 11.0
    costs = CostAssumptions(
        commission_rate=0.0,
        min_commission=0.0,
        stamp_tax_rate=0.0,
        transfer_fee_rate=0.0,
        slippage_rate=0.0,
    )
    trades, _ = simulate_symbol_frame(df, costs=costs)
    assert abs(trades[0]["net_return"] - 0.1) < 1e-12


if __name__ == "__main__":
    test_tdx_sma_matches_recurrence()
    test_kdj_hand_values()
    test_flat_range_does_not_invent_rsv()
    test_groups_do_not_leak()
    test_buy_requires_all_strict_conditions()
    test_sell_branches()
    test_fills_next_open_and_costs()
    test_limit_up_open_cancels_entry()
    test_limit_down_defers_exit()
    test_open_position_marked_at_last_close()
    test_position_day_path_zero_cost()
    test_zero_costs_object_matches_price_ratio()
    print("ok")
