"""R3：decision_scope 与 weekday profile 不含全市场 quotes。"""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from common.decision_scope import decision_quote_symbols, split_hs_bj
from scripts.run_daily_refresh import PROFILE_ARGV


def test_split_hs_bj() -> None:
    conn = MagicMock()
    conn.execute.return_value.fetchall.return_value = [
        ("000001", "sz"),
        ("920165", "bj"),
        ("600000", "sh"),
    ]
    hs, bj = split_hs_bj(conn, ["000001", "920165", "600000", "999999"])
    assert set(hs) == {"000001", "600000", "999999"}
    assert bj == ["920165"]


def test_decision_symbols_union() -> None:
    conn = MagicMock()

    def _exec(sql, params=None):
        cur = MagicMock()
        s = str(sql)
        if "FROM positions" in s:
            cur.fetchall.return_value = [("000001",)]
        elif "FROM paper_positions" in s:
            cur.fetchall.return_value = [("600000",)]
        elif "FROM advice_log" in s:
            cur.fetchall.return_value = [("300001",), ("000001",)]
        else:
            cur.fetchall.return_value = []
        return cur

    conn.execute.side_effect = _exec
    syms = decision_quote_symbols(conn)
    assert syms == ["000001", "300001", "600000"]


def test_weekday_profile_no_full_quotes() -> None:
    only = PROFILE_ARGV["weekday_decision"][1]
    assert "quotes_decision" in only
    assert only.split(",")[1] != "quotes" or "quotes_decision" in only
    assert "quotes_decision" in only
    # 不得默认全市场 quotes / 全量 bj 轻量步
    parts = set(only.split(","))
    assert "quotes" not in parts
    assert "tushare_bse_quotes" not in parts
    assert "tushare_market_data" in parts
    weekend = set(PROFILE_ARGV["weekend_research"][1].split(","))
    assert "quotes" in weekend


def main() -> None:
    test_split_hs_bj()
    test_decision_symbols_union()
    test_weekday_profile_no_full_quotes()
    print("OK: test_decision_scope_profile passed")


if __name__ == "__main__":
    main()
