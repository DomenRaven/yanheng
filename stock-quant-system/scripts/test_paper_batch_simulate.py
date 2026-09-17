"""M9：批量模拟建议成交 + 幂等（临时 DuckDB）。"""
from __future__ import annotations

import datetime as dt
import os
import sys
import tempfile
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT))

import duckdb

from advice.paper_broker import create_paper_account, simulate_advice_cards
from common.db import init_schema
from scripts.test_paper_broker import _seed


def _with_temp_db() -> str:
    path = Path(tempfile.mkdtemp()) / "paper_batch_test.duckdb"
    os.environ["STOCK_QUANT_DB"] = str(path)
    conn = duckdb.connect(str(path))
    init_schema(conn)
    _seed(conn)
    conn.close()
    return str(path)


def main() -> None:
    path = _with_temp_db()
    try:
        aid = create_paper_account("live_prep_10k")
        cards = [
            {
                "advice_id": "adv-batch-1",
                "symbol": "000001",
                "action": "open",
                "size_shares": 100,
                "exec_date": "2026-09-16",
            }
        ]
        s1 = simulate_advice_cards(aid, cards, as_of_nav=dt.date(2026, 9, 16))
        assert s1.filled == 1, s1
        s2 = simulate_advice_cards(aid, cards, as_of_nav=dt.date(2026, 9, 16))
        assert s2.filled == 0 and s2.skipped >= 1, s2
        print("OK batch sim", s1.nav_before, "->", s1.nav_after, "idempotent skip", s2.skipped)
    finally:
        os.environ.pop("STOCK_QUANT_DB", None)
        Path(path).unlink(missing_ok=True)
    print("test_paper_batch_simulate: passed")


if __name__ == "__main__":
    main()
