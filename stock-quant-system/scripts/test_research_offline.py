"""离线单测：不打开 warehouse.duckdb，用合成序列验证研究层。"""
from __future__ import annotations

import datetime as dt

import numpy as np
import pandas as pd

from research.a_share_rules import (
    ST_LIMIT_CHANGE_DATE,
    apply_costs,
    is_limit_locked,
    is_too_new,
    limit_pct,
    round_trip_cost_rate,
)
from research.factors import compute_price_factors, mom_12_1
from research.labeling import triple_barrier
from research.validation import purged_kfold_splits, walk_forward_splits


def test_limit_rules() -> None:
    assert limit_pct("main", False, dt.date(2026, 8, 1)) == 0.10
    assert limit_pct("main", True, dt.date(2026, 7, 5)) == 0.05
    assert limit_pct("main", True, ST_LIMIT_CHANGE_DATE) == 0.10
    assert limit_pct("gem", True, dt.date(2026, 8, 1)) == 0.20
    assert limit_pct("bse", False, dt.date(2026, 8, 1)) == 0.30
    up, down = is_limit_locked(10.0, 11.0, "main", False, dt.date(2026, 8, 1))
    assert up and not down
    assert is_too_new(dt.date(2026, 8, 1), dt.date(2026, 8, 20), 60)
    assert not is_too_new(dt.date(2026, 1, 1), dt.date(2026, 8, 1), 60)


def test_costs() -> None:
    buy = apply_costs(100_000, "buy", "sh")
    sell = apply_costs(100_000, "sell", "sh")
    sell_bj = apply_costs(100_000, "sell", "bj")
    assert sell > buy  # 沪市卖出含印花税
    assert sell_bj < sell  # 北交所暂免印花税
    assert round_trip_cost_rate("sh") > round_trip_cost_rate("bj")


def test_factors_and_labels() -> None:
    n = 300
    rng = np.random.default_rng(42)
    rets = rng.normal(0.0005, 0.02, n)
    close = 10 * np.cumprod(1 + rets)
    df = pd.DataFrame(
        {
            "close": close,
            "high": close * 1.01,
            "low": close * 0.99,
            "turnover": rng.uniform(0.5, 3.0, n),
        }
    )
    out = compute_price_factors(df)
    assert out["factor_mom_12_1"].notna().sum() > 0
    assert np.isfinite(mom_12_1(df["close"]).iloc[-1])
    labels = triple_barrier(df["close"], profit_take=0.03, stop_loss=0.03, max_holding=5, vol_adjust=False)
    assert set(labels["label"].unique()).issubset({-1, 0, 1})
    assert labels["touch_horizon"].notna().sum() > n * 0.8


def test_validation_no_overlap() -> None:
    n, horizon, embargo = 100, 10, 5
    for train, test in purged_kfold_splits(n, n_splits=5, label_horizon=horizon, embargo=embargo):
        assert len(np.intersect1d(train, test)) == 0
        # 训练集不应落入 [test_start - horizon, test_end + embargo)
        t0, t1 = int(test.min()), int(test.max()) + 1
        forbidden = set(range(max(0, t0 - horizon), min(n, t1 + embargo)))
        assert forbidden.isdisjoint(set(train.tolist()))
    wfs = list(walk_forward_splits(200, train_size=80, test_size=20, step=20))
    assert len(wfs) >= 5
    for train, test in wfs:
        assert train.max() < test.min()


if __name__ == "__main__":
    test_limit_rules()
    test_costs()
    test_factors_and_labels()
    test_validation_no_overlap()
    print("offline research tests: all passed")
