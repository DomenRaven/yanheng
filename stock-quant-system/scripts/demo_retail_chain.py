"""散户决策链 E2E 演示（临时库）：Top 候选卡片 → 批量模拟 → 净值变化 + 幂等。

不依赖 warehouse.duckdb；不跑全市场 scanner。用于验收 M9「候选→成交→净值」语义。
"""
from __future__ import annotations

import datetime as dt
import os
import sys
import tempfile
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT))

import duckdb

from advice.advice_engine import enrich_cards_with_sizing
from advice.paper_broker import create_paper_account, simulate_advice_cards
from common.db import init_schema
from scripts.test_paper_broker import _seed


def _with_temp_db() -> str:
    path = Path(tempfile.mkdtemp()) / "retail_chain_demo.duckdb"
    os.environ["STOCK_QUANT_DB"] = str(path)
    conn = duckdb.connect(str(path))
    init_schema(conn)
    _seed(conn)
    conn.close()
    return str(path)


def main() -> None:
    path = _with_temp_db()
    try:
        from common.db import get_connection

        conn = get_connection()
        init_schema(conn)
        signal = dt.date(2026, 9, 15)
        exec_d = dt.date(2026, 9, 16)
        import pandas as pd

        position_summary = pd.DataFrame(
            columns=["symbol", "shares", "cost_value", "market_value", "cost_price", "last_close"]
        )
        cards = [
            {
                "advice_id": "demo-open-1",
                "symbol": "000001",
                "name": "平安",
                "action": "open",
                "confidence": 0.8,
                "rank": 3,
                "price_levels": {
                    "last_close": 10.0,
                    "stop_loss_price": 9.2,
                    "take_profit_price": 10.8,
                },
                "reasons": [{"type": "model", "detail": "模型打分 0.9，全市场排名第3"}],
                "risks": [],
                "invalid_if": [],
            }
        ]
        enriched = enrich_cards_with_sizing(
            conn,
            cards,
            signal,
            position_summary,
            set(),
            {"000001": "sz"},
        )
        conn.close()
        assert enriched[0].get("size_shares", 0) >= 100, enriched[0]
        assert enriched[0].get("exec_date") == exec_d.isoformat()
        assert enriched[0].get("invalid_if"), enriched[0]
        assert "排名大幅下降" not in "".join(enriched[0]["invalid_if"])

        aid = create_paper_account("live_prep_10k")
        s1 = simulate_advice_cards(aid, enriched, as_of_nav=exec_d)
        assert s1.filled >= 1, s1
        s2 = simulate_advice_cards(aid, enriched, as_of_nav=exec_d)
        assert s2.skipped >= 1 and s2.filled == 0, s2
        print(
            "OK retail chain: open",
            enriched[0]["size_shares"],
            "shares, nav",
            f"{s1.nav_before:.2f} -> {s1.nav_after:.2f},",
            "second run idempotent",
        )
    finally:
        os.environ.pop("STOCK_QUANT_DB", None)
        Path(path).unlink(missing_ok=True)
    print("demo_retail_chain: passed")


if __name__ == "__main__":
    main()
