"""research/live_performance 离线单测：合成数据，不连库。

- 单调情形：分数越高未来涨越多 → RankIC ≈ 1，残差 RankIC 仍接近 1。
- 纯价格效应：分数与收益都只由价格决定 → 原始 RankIC 显著非零，控制 log 价格后残差 RankIC ≈ 0。
- 远期收益截断：数据末尾不足 h 日时 effective 被截断。
- 建议卡片结果：收益 / ±8% 命中 / 基准 / 超额 可手算核对。
- 独立窗口：重叠快照只数一个。
"""
from __future__ import annotations

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT))

import numpy as np
import pandas as pd

from research.live_performance import (
    advice_outcomes,
    decile_table,
    forward_returns,
    independent_windows,
    price_confound,
    rank_ic,
)


def _quotes_from_wide(px: pd.DataFrame) -> pd.DataFrame:
    long = px.stack().reset_index()
    long.columns = ["trade_date", "symbol", "close"]
    return long


def _dates(n: int) -> pd.DatetimeIndex:
    return pd.bdate_range("2026-09-01", periods=n)


def test_forward_returns_and_clipping() -> None:
    dates = _dates(6)  # 快照后只有 5 个交易日
    px = pd.DataFrame(
        {"A": [10, 11, 12, 13, 14, 15], "B": [20, 20, 20, 20, 20, 20]}, index=dates
    ).astype(float)
    fr = forward_returns(_quotes_from_wide(px), dates[0], horizons=(1, 3, 10))
    assert fr.effective == {1: 1, 3: 3, 10: 5}, fr.effective
    assert fr.days_after == 5
    assert abs(fr.frame.loc["A", "fwd_1"] - 0.1) < 1e-12
    assert abs(fr.frame.loc["A", "fwd_10"] - 0.5) < 1e-12  # 截断到最后一日 15/10-1
    assert fr.frame.loc["B", "fwd_3"] == 0.0
    # 停牌：B 缺最后两天，用前值填充 → 收益与第 4 日一致
    px2 = px.copy()
    px2.loc[dates[4:], "B"] = np.nan
    px2.loc[dates[3], "B"] = 22.0
    fr2 = forward_returns(_quotes_from_wide(px2), dates[0], horizons=(10,))
    assert abs(fr2.frame.loc["B", "fwd_10"] - 0.1) < 1e-12
    assert bool(fr2.frame.loc["B", "real_10"]) is False and bool(fr2.frame.loc["A", "real_10"]) is True
    assert abs(fr2.coverage[10] - 0.5) < 1e-12
    assert fr.coverage == {1: 1.0, 3: 1.0, 10: 1.0}


def test_rank_ic_monotone() -> None:
    rng = np.random.default_rng(0)
    n = 500
    score = pd.Series(rng.normal(size=n))
    fwd = score * 0.05  # 完全单调
    price = pd.Series(rng.lognormal(mean=3.0, sigma=0.8, size=n))
    ic = rank_ic(score, fwd)
    assert abs(ic["rank_ic"] - 1.0) < 1e-9 and ic["n"] == n and ic["p_value"] < 1e-6
    diag = price_confound(score, fwd, price)
    assert diag["residual_rank_ic"] > 0.95, diag["residual_rank_ic"]
    assert diag["quintile_mean_rank_ic"] > 0.95
    dec = decile_table(score, fwd, price)
    assert len(dec) == 10 and dec["mean_fwd"].is_monotonic_increasing
    # 常数列返回 NaN，不报错
    assert np.isnan(rank_ic(pd.Series([1.0, 1.0, 1.0]), pd.Series([0.1, 0.2, 0.3]))["rank_ic"])


def test_pure_price_effect_residual_near_zero() -> None:
    rng = np.random.default_rng(1)
    n = 3000
    log_px = rng.normal(loc=3.0, scale=0.8, size=n)
    price = pd.Series(np.exp(log_px))
    # 分数偏好低价股；收益也只由低价决定（低价涨多）；两者除价格外互相独立
    score = pd.Series(-log_px + rng.normal(scale=0.3, size=n))
    fwd = pd.Series(-0.05 * log_px + rng.normal(scale=0.03, size=n))
    raw = rank_ic(score, fwd)["rank_ic"]
    diag = price_confound(score, fwd, price)
    assert raw > 0.5, raw                                  # 表面上像有选股能力
    assert diag["spearman_price_fwd"] < -0.5
    assert diag["spearman_rank_price"] > 0.5               # 名次越靠前（数字小）价格越低
    assert abs(diag["residual_rank_ic"]) < 0.05, diag["residual_rank_ic"]
    # 五分位是粗控制：组内价格仍有差异，只能把相关压掉一半以上，压不到 0
    assert abs(diag["quintile_mean_rank_ic"]) < 0.5 * raw, (diag["quintile_mean_rank_ic"], raw)
    assert diag["p_residual"] > 0.01


def test_advice_outcomes_hand_check() -> None:
    dates = _dates(4)
    px = pd.DataFrame(
        {
            "000001": [10.0, 10.5, 11.0, 11.0],   # +10% 期末命中止盈
            "000002": [10.0, 9.0, 9.5, 9.6],      # 期末 -4%，路径最低 -10% 触及止损
            "830001": [5.0, 5.0, 5.0, 5.5],       # 北交所 +10%
            "600000": [20.0, 20.0, 20.0, 19.0],   # 不在卡片里，只进等权 -5%
            "600001": [30.0, 33.0, np.nan, np.nan],  # 期末没有真实行情：不进卡片均值也不进等权
        },
        index=dates,
    )
    quotes = _quotes_from_wide(px)
    idx = pd.concat(
        [
            pd.DataFrame({"index_code": "sh000300", "trade_date": dates, "close": [100, 101, 102, 98.0]}),
            pd.DataFrame({"index_code": "sh000852", "trade_date": dates, "close": [100, 100, 100, 104.0]}),
        ]
    )
    universe = pd.DataFrame(
        {
            "symbol": ["000001", "000002", "830001", "600000", "600001"],
            "exchange": ["sz", "sz", "bj", "sh", "sh"],
        }
    )
    cards = pd.DataFrame(
        {
            "as_of": [dates[0]] * 4 + [dates[3]],
            "pool_id": ["hs", "hs", "hs", "bj", "hs"],
            "symbol": ["000001", "000002", "600001", "830001", "600000"],
            "action": ["open", "open", "open", "open", "open"],
        }
    )
    summary, detail = advice_outcomes(cards, quotes, idx, universe)
    hs = summary[(summary["pool_id"] == "hs") & (summary["as_of"] == dates[0].date().isoformat())].iloc[0]
    assert hs["n_cards"] == 3 and hs["n_priced"] == 2 and hs["n_stale"] == 1 and hs["days_held"] == 3
    assert abs(hs["mean_ret"] - 0.03) < 1e-12           # (+10% - 4%) / 2
    assert hs["hit_tp"] == 0.5 and hs["hit_sl"] == 0.0 and hs["touched_sl"] == 0.5
    assert abs(hs["hs300_ret"] - (-0.02)) < 1e-12
    assert abs(hs["excess_vs_hs300"] - 0.05) < 1e-12
    assert abs(hs["ew_pool_ret"] - ((0.10 - 0.04 - 0.05) / 3)) < 1e-12  # 沪深三只等权
    bj = summary[summary["pool_id"] == "bj"].iloc[0]
    assert abs(bj["mean_ret"] - 0.10) < 1e-12 and abs(bj["csi1000_ret"] - 0.04) < 1e-12
    # 快照日就是数据末尾：持有 0 日，收益为 NaN
    tail = summary[summary["as_of"] == dates[3].date().isoformat()].iloc[0]
    assert tail["days_held"] == 0 and np.isnan(tail["mean_ret"])
    assert len(detail) == 4 and int(detail["real_end"].sum()) == 3


def test_independent_windows() -> None:
    dates = _dates(40)
    snaps = [dates[0], dates[1], dates[5], dates[12], dates[30], dates[39]]
    res = independent_windows(snaps, dates, window=10)
    # 0 → 12 → 30 (39 与 30 只隔 9 日，不独立)
    assert res["n_independent"] == 3, res
    assert res["n_complete"] == 2, res  # 30+10=40 超出末尾 39，不算走满


def main() -> None:
    test_forward_returns_and_clipping()
    test_rank_ic_monotone()
    test_pure_price_effect_residual_near_zero()
    test_advice_outcomes_hand_check()
    test_independent_windows()
    print("ALL ASSERTIONS PASSED")


if __name__ == "__main__":
    main()
