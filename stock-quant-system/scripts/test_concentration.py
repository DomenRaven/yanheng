"""集中度：6 位代码必须是字符串类别轴，不能被当成「60万」这种数值。"""
from __future__ import annotations

import pandas as pd
import plotly.express as px

from risk.portfolio_risk import _canon_symbol, compute_concentration


def test_canon_symbol_int_and_padded() -> None:
    assert _canon_symbol(600519) == "600519"
    assert _canon_symbol(1) == "000001"
    assert _canon_symbol("000001") == "000001"
    assert _canon_symbol(600519.0) == "600519"


def test_concentration_symbols_are_six_digit_strings() -> None:
    positions = pd.DataFrame({
        "symbol": [600519, 1, 600519],
        "market_value": [200.0, 100.0, 82.0],
    })
    out = compute_concentration(positions)
    assert list(out["symbol"]) == ["600519", "000001"]
    w519 = float(out.loc[out["symbol"] == "600519", "weight_pct"].iloc[0])
    assert abs(w519 - 282 / 382) < 1e-12


def test_category_axis_prevents_wan_labels() -> None:
    df = pd.DataFrame({"weight_pct": [0.282, 0.10], "symbol": ["600519", "000001"]})
    fig = px.bar(df, x="weight_pct", y="symbol", orientation="h")
    fig.update_yaxes(type="category")
    assert fig.layout.yaxis.type == "category"
    y_vals = list(fig.data[0].y)
    assert y_vals == ["600519", "000001"]


if __name__ == "__main__":
    test_canon_symbol_int_and_padded()
    test_concentration_symbols_are_six_digit_strings()
    test_category_axis_prevents_wan_labels()
    print("concentration tests: all passed")
