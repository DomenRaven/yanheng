"""S2：开仓待办同一行业最多 1 只（临时库）。"""
from __future__ import annotations

import datetime as dt
import os
import sys
import tempfile
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT))

import duckdb

from advice.entry_rules import build_tomorrow_todos
from advice.industry_cap import cap_opens_one_per_industry, latest_industry_codes
from common.db import init_schema


def _with_temp() -> str:
    path = Path(tempfile.mkdtemp()) / "industry_cap_test.duckdb"
    os.environ["STOCK_QUANT_DB"] = str(path)
    conn = duckdb.connect(str(path))
    init_schema(conn)
    conn.execute(
        """
        INSERT INTO industry_classification (symbol, start_date, industry_code)
        VALUES
          ('000001', DATE '2020-01-01', 'SW1'),
          ('000002', DATE '2020-01-01', 'SW1'),
          ('600000', DATE '2020-01-01', 'SW2')
        """
    )
    for td in (dt.date(2026, 9, 15), dt.date(2026, 9, 16)):
        conn.execute("INSERT INTO trade_calendar (trade_date) VALUES (?)", [td])
    conn.close()
    return str(path)


def main() -> None:
    # 纯函数：同行业两只 open，只留排名靠前的一只，再收下不同行业
    items = [
        {"symbol": "000001", "action": "open", "confidence": 0.9},
        {"symbol": "000002", "action": "open", "confidence": 0.8},
        {"symbol": "600000", "action": "open", "confidence": 0.7},
    ]
    mapping = {"000001": "SW1", "000002": "SW1", "600000": "SW2"}
    picked = cap_opens_one_per_industry(items, mapping, max_items=3)
    assert [p["symbol"] for p in picked] == ["000001", "600000"], picked
    assert picked[0]["industry_code"] == "SW1"

    # 止损不受行业上限拦截
    mixed = [
        {"symbol": "000001", "action": "stop_loss"},
        {"symbol": "000002", "action": "open"},
        {"symbol": "600000", "action": "open"},
    ]
    mixed_map = {"000001": "SW1", "000002": "SW1", "600000": "SW2"}
    picked2 = cap_opens_one_per_industry(mixed, mixed_map, max_items=3)
    assert [p["symbol"] for p in picked2] == ["000001", "000002", "600000"], picked2

    path = _with_temp()
    try:
        from common.db import get_connection

        conn = get_connection()
        as_of = dt.date(2026, 9, 15)
        codes = latest_industry_codes(conn, ["000001", "000002", "600000"], as_of)
        assert codes["000001"] == codes["000002"] == "SW1"
        assert codes["600000"] == "SW2"
        cards = [
            {"advice_id": "a", "symbol": "000001", "action": "open", "confidence": 0.9, "size_shares": 100},
            {"advice_id": "b", "symbol": "000002", "action": "open", "confidence": 0.8, "size_shares": 100},
            {"advice_id": "c", "symbol": "600000", "action": "open", "confidence": 0.7, "size_shares": 100},
        ]
        todos = build_tomorrow_todos(cards, as_of, conn, max_items=3)
        syms = [t["symbol"] for t in todos]
        assert "000001" in syms
        assert "000002" not in syms, f"同行业第二只不应进待办: {syms}"
        assert "600000" in syms
        conn.close()
        print("OK S2 industry cap", syms)
    finally:
        os.environ.pop("STOCK_QUANT_DB", None)
        Path(path).unlink(missing_ok=True)
    print("test_industry_cap: passed")


if __name__ == "__main__":
    main()
