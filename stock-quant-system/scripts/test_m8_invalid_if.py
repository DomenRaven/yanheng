"""规格 M8：invalid_if 必须可盘中/次日核对，禁止空话。"""
from __future__ import annotations

import datetime as dt
import os
import sys
import tempfile
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT))

import duckdb
import pandas as pd

from advice.advice_engine import (
    build_advice_for_positions,
    build_advice_for_watchlist,
    enrich_cards_with_sizing,
    load_latest_advice_cards,
    persist_advice_cards,
)
from advice.position_sizing import (
    EMPTY_TALK_TOKENS,
    actionable_invalid_if_hold,
    actionable_invalid_if_open,
    actionable_invalid_if_watch,
    invalid_if_is_actionable,
)
from common.db import init_schema
from scripts.test_paper_broker import _seed


def _assert_actionable(items: list[str], *, label: str) -> None:
    assert invalid_if_is_actionable(items), f"{label} 不可核对: {items}"
    blob = "".join(items)
    for tok in EMPTY_TALK_TOKENS:
        assert tok not in blob, f"{label} 含空话 {tok!r}: {items}"


def test_helpers() -> None:
    _assert_actionable(actionable_invalid_if_open(9.2), label="open")
    _assert_actionable(actionable_invalid_if_watch(), label="watch")
    _assert_actionable(actionable_invalid_if_hold(stop_price=9.2), label="hold")
    assert not invalid_if_is_actionable([])
    assert not invalid_if_is_actionable(["模型下一次重新打分排名大幅下降"])
    print("OK helpers")


def test_watchlist_and_positions_cards() -> None:
    scan = pd.DataFrame(
        [
            {
                "symbol": "000001",
                "name": "平安",
                "rank": 3,
                "pred_score": 0.2,
                "close": 10.0,
                "is_tradable": True,
                "factor_roe": 0.1,
                "factor_mom_12_1": 0.05,
                "conflict_flag": "无冲突",
            },
            {
                "symbol": "000002",
                "name": "万科",
                "rank": 80,
                "pred_score": 0.01,
                "close": 8.0,
                "is_tradable": True,
                "factor_roe": 0.05,
                "factor_mom_12_1": 0.0,
                "conflict_flag": "无冲突",
            },
        ]
    )
    cards = build_advice_for_watchlist(scan)
    assert cards[0]["action"] == "open"
    assert cards[1]["action"] == "watch"
    for c in cards:
        _assert_actionable(c["invalid_if"], label=f"watchlist {c['symbol']} {c['action']}")

    pos = pd.DataFrame(
        [
            {
                "symbol": "600000",
                "shares": 100,
                "cost_value": 1000.0,
                "market_value": 1100.0,
                "cost_price": 10.0,
                "last_close": 11.0,
            }
        ]
    )
    scan_full = pd.DataFrame(
        [{"symbol": "600000", "name": "浦发", "pred_score": 0.3, "rank": 10, "is_tradable": True}]
    )
    conc = pd.DataFrame([{"symbol": "600000", "weight_pct": 0.11}])
    holds = build_advice_for_positions(pos, scan_full, conc)
    assert holds[0]["action"] == "hold"
    _assert_actionable(holds[0]["invalid_if"], label="hold card")
    print("OK builders")


def test_enrich_fills_empty_and_no_stop() -> None:
    path = Path(tempfile.mkdtemp()) / "m8.duckdb"
    os.environ["STOCK_QUANT_DB"] = str(path)
    conn = duckdb.connect(str(path))
    try:
        init_schema(conn)
        _seed(conn)
        empty = pd.DataFrame(
            columns=["symbol", "shares", "cost_value", "market_value", "cost_price", "last_close"]
        )
        cards = [
            {
                "advice_id": "m8-open-empty",
                "symbol": "000001",
                "action": "open",
                "confidence": 0.8,
                "price_levels": {
                    "last_close": 10.0,
                    "stop_loss_price": 9.2,
                    "take_profit_price": 10.8,
                },
                "reasons": [],
                "risks": [],
                "invalid_if": ["模型下一次重新打分排名大幅下降"],
            },
            {
                "advice_id": "m8-no-stop",
                "symbol": "000001",
                "action": "open",
                "confidence": 0.7,
                "price_levels": {"last_close": 10.0, "stop_loss_price": None},
                "reasons": [],
                "risks": [],
                "invalid_if": [],
            },
        ]
        out = enrich_cards_with_sizing(
            conn,
            cards,
            dt.date(2026, 9, 15),
            empty,
            set(),
            {"000001": "sz"},
        )
        filled = {c["advice_id"]: c for c in out}
        _assert_actionable(filled["m8-open-empty"]["invalid_if"], label="enrich rewrite")
        assert filled["m8-no-stop"]["action"] == "watch"
        _assert_actionable(filled["m8-no-stop"]["invalid_if"], label="no-stop→watch")
        print("OK enrich rewrite")
    finally:
        conn.close()
        os.environ.pop("STOCK_QUANT_DB", None)
        path.unlink(missing_ok=True)


def test_persist_reason_and_invalid_if() -> None:
    path = Path(tempfile.mkdtemp()) / "m8persist.duckdb"
    os.environ["STOCK_QUANT_DB"] = str(path)
    conn = duckdb.connect(str(path))
    try:
        init_schema(conn)
        _seed(conn)
        scan = pd.DataFrame(
            [
                {
                    "symbol": "000001",
                    "name": "平安",
                    "rank": 2,
                    "pred_score": 0.2,
                    "close": 10.0,
                    "is_tradable": True,
                    "factor_roe": 0.1,
                    "factor_mom_12_1": 0.05,
                    "conflict_flag": "无冲突",
                }
            ]
        )
        cards = build_advice_for_watchlist(scan)
        persist_advice_cards(conn, cards, "2026-09-15", "20260826_123439")
        as_of, loaded = load_latest_advice_cards(conn)
        assert as_of
        assert loaded[0]["reason_one_liner"]
        _assert_actionable(loaded[0]["invalid_if"], label="persisted")
        print("OK persist reason_one_liner + invalid_if")
    finally:
        conn.close()
        os.environ.pop("STOCK_QUANT_DB", None)
        path.unlink(missing_ok=True)


if __name__ == "__main__":
    test_helpers()
    test_watchlist_and_positions_cards()
    test_enrich_fills_empty_and_no_stop()
    test_persist_reason_and_invalid_if()
    print("test_m8_invalid_if: passed")
