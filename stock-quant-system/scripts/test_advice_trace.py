"""M16：advice_id 全链路（临时库）。persist → 模拟成交 → JOIN 可追溯。"""
from __future__ import annotations

import datetime as dt
import os
import sys
import tempfile
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT))

import duckdb

from advice.advice_engine import persist_advice_cards
from advice.advice_trace import complete_trace_count, load_advice_traces
from advice.paper_broker import create_paper_account, execute_paper_order
from common.db import init_schema, write_session
from scripts.test_paper_broker import _seed


def _with_temp_db() -> str:
    path = Path(tempfile.mkdtemp()) / "advice_trace_test.duckdb"
    os.environ["STOCK_QUANT_DB"] = str(path)
    conn = duckdb.connect(str(path))
    init_schema(conn)
    _seed(conn)
    conn.close()
    return str(path)


def main() -> None:
    path = _with_temp_db()
    try:
        as_of = dt.date(2026, 9, 15)
        exec_d = dt.date(2026, 9, 16)
        card = {
            "advice_id": "adv-trace-1",
            "symbol": "000001",
            "name": "平安",
            "action": "open",
            "confidence": 0.6,
            "plain_summary": "测试全链路",
            "price_levels": {"last_close": 10.5, "stop_loss_price": 9.66},
            "reasons": [{"type": "factor", "ref": "ROE", "detail": "test"}],
            "risks": [],
            "invalid_if": ["次日开盘涨停"],
            "size_shares": 100,
            "est_amount_cny": 1000.0,
            "size_pct_nav": 0.1,
            "max_loss_cny": 84.0,
            "horizon_days": 20,
            "exec_date": exec_d,
        }
        with write_session(init=True) as conn:
            persist_advice_cards(conn, [card], str(as_of), model_run_id="test-run")

        account = create_paper_account("live_prep_10k")
        filled = execute_paper_order(
            account,
            symbol="000001",
            side="buy",
            shares=100,
            trade_date=exec_d,
            advice_id="adv-trace-1",
            source="sim",
        )
        assert filled.status == "filled", filled

        with write_session() as conn:
            empty = load_advice_traces(conn, advice_id="does-not-exist")
            assert empty.empty, empty
            traces = load_advice_traces(conn, advice_id="adv-trace-1")
            all_rows = load_advice_traces(conn)

        assert len(traces) == 1, traces
        row = traces.iloc[0]
        assert bool(row["trace_complete"]) is True
        assert row["advice_id"] == "adv-trace-1"
        assert int(row["planned_shares"]) == 100
        assert int(row["fill_shares"]) == 100
        assert abs(float(row["shares_gap"])) < 1e-9
        assert int(row["planned_shares"]) == int(row["fill_shares"])
        assert row["fill_source"] == "sim"
        assert pd_notna_close(row["advice_close"]) and abs(float(row["advice_close"]) - 10.5) < 1e-9
        assert pd_notna_close(row["fill_vs_advice_close"])
        # 次日开盘 10.0 ± 滑点，相对建议日收盘 10.5 应为折价（执行缺口，不是 alpha）
        assert float(row["fill_vs_advice_close"]) < 0, row["fill_vs_advice_close"]
        assert complete_trace_count(all_rows) == 1
        print(
            "OK M16 trace",
            row["advice_id"],
            "fill_vs_close",
            f"{float(row['fill_vs_advice_close']):+.4%}",
        )
    finally:
        os.environ.pop("STOCK_QUANT_DB", None)
        Path(path).unlink(missing_ok=True)
    print("test_advice_trace: passed")


def pd_notna_close(val) -> bool:
    import pandas as pd

    return bool(pd.notna(val))


if __name__ == "__main__":
    main()
