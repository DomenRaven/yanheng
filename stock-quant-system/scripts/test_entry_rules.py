"""Phase 5 阶段 C：entry_rules 轻量测例（需本地 DuckDB 有 trade_cal）。"""
from __future__ import annotations

import datetime as dt
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from advice.entry_rules import build_tomorrow_todos, next_trade_date
from common.db import get_connection, init_schema


def main() -> None:
    conn = get_connection()
    init_schema(conn)
    try:
        sig = dt.date(2026, 9, 15)
        nxt = next_trade_date(conn, sig)
        assert nxt is not None and nxt > sig, nxt
        cards = [
            {
                "advice_id": "a1",
                "symbol": "000001",
                "name": "测试",
                "action": "open",
                "confidence": 0.8,
                "size_shares": 100,
                "est_amount_cny": 1200.0,
                "plain_summary": "test",
            }
        ]
        todos = build_tomorrow_todos(cards, sig, conn, max_items=3)
        assert len(todos) <= 3
        if todos:
            assert todos[0]["exec_date"] == nxt.isoformat()
        print("OK next_trade_date", nxt, "todos", len(todos))
    finally:
        conn.close()
    print("test_entry_rules: passed")


if __name__ == "__main__":
    main()
