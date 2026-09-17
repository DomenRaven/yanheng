"""Phase 5 阶段 A：paper_broker 验收（临时 DuckDB，不碰 warehouse）。"""
from __future__ import annotations

import datetime as dt
import os
import sys
import tempfile
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT))

import duckdb

from advice.paper_broker import create_paper_account, execute_paper_order, mark_to_market_nav
from common.db import init_schema, write_session
from research.a_share_rules import CostAssumptions


def _seed(conn: duckdb.DuckDBPyConnection) -> None:
    conn.execute("INSERT INTO universe (symbol, name, exchange, is_st, is_delisted) VALUES ('000001', '平安', 'sz', false, false)")
    d = dt.date(2026, 9, 15)
    conn.execute(
        """
        INSERT INTO daily_quotes (symbol, trade_date, open, close, adjust)
        VALUES ('000001', ?, 10.0, 10.5, 'qfq')
        """,
        [d],
    )
    conn.execute(
        """
        INSERT INTO limit_price (symbol, trade_date, up_limit, down_limit)
        VALUES ('000001', ?, 11.0, 9.0)
        """,
        [d],
    )
    d2 = dt.date(2026, 9, 16)
    conn.execute(
        """
        INSERT INTO daily_quotes (symbol, trade_date, open, close, adjust)
        VALUES ('000001', ?, 10.0, 10.2, 'qfq')
        """,
        [d2],
    )
    conn.execute(
        """
        INSERT INTO limit_price (symbol, trade_date, up_limit, down_limit)
        VALUES ('000001', ?, 11.0, 9.0)
        """,
        [d2],
    )
    for td in (d, d2):
        conn.execute(
            "INSERT INTO trade_calendar (trade_date) VALUES (?)",
            [td],
        )


def _with_temp_db() -> str:
    path = Path(tempfile.mkdtemp()) / "paper_test.duckdb"
    os.environ["STOCK_QUANT_DB"] = str(path)
    conn = duckdb.connect(str(path))
    init_schema(conn)
    _seed(conn)
    conn.close()
    return str(path)


def test_lot_size_and_buy_sell_tplus1() -> None:
    path = _with_temp_db()
    try:
        aid = create_paper_account("live_prep_10k")
        d0 = dt.date(2026, 9, 15)
        bad = execute_paper_order(aid, symbol="000001", side="buy", shares=50, trade_date=d0)
        assert bad.status == "rejected" and "100" in (bad.reject_reason or "")

        buy = execute_paper_order(
            aid, symbol="000001", side="buy", shares=100, trade_date=d0, advice_id="adv-1"
        )
        assert buy.status == "filled", buy
        assert buy.cash_after is not None and buy.cash_after < 10_000.0

        sell_same = execute_paper_order(aid, symbol="000001", side="sell", shares=100, trade_date=d0)
        assert sell_same.status == "rejected" and "T+1" in (sell_same.reject_reason or "")

        d1 = dt.date(2026, 9, 16)
        sell = execute_paper_order(aid, symbol="000001", side="sell", shares=100, trade_date=d1, advice_id="adv-2")
        assert sell.status == "filled", sell

        with write_session() as conn:
            nav = mark_to_market_nav(conn, aid, d1)
        assert nav["cash_cny"] > sell.cash_after - 1  # type: ignore[operator]
    finally:
        os.environ.pop("STOCK_QUANT_DB", None)
        Path(path).unlink(missing_ok=True)


def test_idempotent_advice_id() -> None:
    path = _with_temp_db()
    try:
        aid = create_paper_account("practice_100k")
        d0 = dt.date(2026, 9, 15)
        r1 = execute_paper_order(aid, symbol="000001", side="buy", shares=100, trade_date=d0, advice_id="adv-x")
        r2 = execute_paper_order(aid, symbol="000001", side="buy", shares=100, trade_date=d0, advice_id="adv-x")
        assert r1.status == "filled"
        assert r2.status == "skipped"
        with write_session() as conn:
            n = conn.execute(
                "SELECT count(*) FROM paper_trades WHERE advice_id = 'adv-x' AND reject_reason IS NULL"
            ).fetchone()[0]
        assert n == 1
    finally:
        os.environ.pop("STOCK_QUANT_DB", None)
        Path(path).unlink(missing_ok=True)


def test_limit_up_rejects_buy() -> None:
    path = _with_temp_db()
    try:
        aid = create_paper_account("live_prep_10k")
        d0 = dt.date(2026, 9, 15)
        # open at up_limit
        with write_session() as conn:
            conn.execute(
                "UPDATE daily_quotes SET open = 11.0 WHERE symbol = '000001' AND trade_date = ?",
                [d0],
            )
        r = execute_paper_order(aid, symbol="000001", side="buy", shares=100, trade_date=d0)
        assert r.status == "rejected" and "涨停" in (r.reject_reason or "")
    finally:
        os.environ.pop("STOCK_QUANT_DB", None)
        Path(path).unlink(missing_ok=True)


def test_min_commission_on_small_buy() -> None:
    path = _with_temp_db()
    try:
        aid = create_paper_account("live_prep_10k")
        d0 = dt.date(2026, 9, 15)
        r = execute_paper_order(
            aid,
            symbol="000001",
            side="buy",
            shares=100,
            trade_date=d0,
            cost_assumptions=CostAssumptions(),
        )
        assert r.status == "filled"
        assert r.fees is not None and r.fees >= 5.0
    finally:
        os.environ.pop("STOCK_QUANT_DB", None)
        Path(path).unlink(missing_ok=True)


def test_cannot_afford_one_lot_on_10k_high_price() -> None:
    path = Path(tempfile.mkdtemp()) / "paper_test2.duckdb"
    os.environ["STOCK_QUANT_DB"] = str(path)
    try:
        conn = duckdb.connect(str(path))
        init_schema(conn)
        conn.execute(
            "INSERT INTO universe (symbol, name, exchange, is_st, is_delisted) VALUES ('600519', '茅台', 'sh', false, false)"
        )
        d0 = dt.date(2026, 9, 15)
        conn.execute(
            """
            INSERT INTO daily_quotes (symbol, trade_date, open, close, adjust)
            VALUES ('600519', ?, 1500.0, 1500.0, 'qfq')
            """,
            [d0],
        )
        conn.execute(
            """
            INSERT INTO limit_price (symbol, trade_date, up_limit, down_limit)
            VALUES ('600519', ?, 1650.0, 1350.0)
            """,
            [d0],
        )
        conn.close()
        aid = create_paper_account("live_prep_10k")
        r = execute_paper_order(aid, symbol="600519", side="buy", shares=100, trade_date=d0)
        assert r.status == "rejected" and "现金" in (r.reject_reason or "")
    finally:
        os.environ.pop("STOCK_QUANT_DB", None)
        path.unlink(missing_ok=True)


def main() -> None:
    test_lot_size_and_buy_sell_tplus1()
    test_idempotent_advice_id()
    test_limit_up_rejects_buy()
    test_min_commission_on_small_buy()
    test_cannot_afford_one_lot_on_10k_high_price()
    print("test_paper_broker: all passed")


if __name__ == "__main__":
    main()
