"""R1：scan_run 去重 + 坚持度查询。"""
from __future__ import annotations

import datetime as dt
import os
import sys
import tempfile
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from advice.scan_archive import (
    persistence_full_attendance,
    persistence_hit_counts,
    record_scan_archive,
    scan_run_exists,
    symbol_rank_history,
)
from common.db import init_schema, write_session


def _seed_ranks(conn) -> None:
    conn.execute(
        "INSERT INTO universe (symbol, name, exchange, is_st, is_delisted) VALUES "
        "('000001', '平安', 'sz', false, false), ('000002', '万科', 'sz', false, false)"
    )
    dates = [dt.date(2026, 9, 10), dt.date(2026, 9, 11), dt.date(2026, 9, 12),
             dt.date(2026, 9, 15), dt.date(2026, 9, 16)]
    for i, d in enumerate(dates):
        ranked = pd.DataFrame(
            {
                "symbol": ["000001", "000002"],
                "rank": [1, 2] if i % 2 == 0 else [2, 1],
                "pred_score": [1.0, 0.5],
                "is_tradable": [True, True],
            }
        )
        # 000001 每次都进前50
        record_scan_archive(
            conn, ranked=ranked, trade_date=d, model_run_id="m1", pool_id="all", source="test"
        )


def main() -> None:
    path = Path(tempfile.mkdtemp()) / "scan_arch.duckdb"
    os.environ["STOCK_QUANT_DB"] = str(path)
    try:
        with write_session(init=True) as conn:
            init_schema(conn)
            _seed_ranks(conn)
            assert scan_run_exists(conn, "all", dt.date(2026, 9, 16), "m1")
            # 同键再写不增 run 数
            n0 = conn.execute("SELECT count(*) FROM scan_run").fetchone()[0]
            ranked = pd.DataFrame(
                {
                    "symbol": ["000001", "000002"],
                    "rank": [1, 2],
                    "pred_score": [1.1, 0.4],
                    "is_tradable": [True, True],
                }
            )
            meta = record_scan_archive(
                conn,
                ranked=ranked,
                trade_date=dt.date(2026, 9, 16),
                model_run_id="m1",
                pool_id="all",
            )
            assert meta["existed"] is True
            n1 = conn.execute("SELECT count(*) FROM scan_run").fetchone()[0]
            assert n1 == n0

            full = persistence_full_attendance(conn, window_runs=5, top_k=50)
            assert not full.empty
            assert "000001" in set(full["symbol"])

            hits = persistence_hit_counts(conn, lookback_runs=20, top_k=50)
            assert not hits.empty
            hist = symbol_rank_history(conn, "000001")
            assert len(hist) >= 5
        print("OK: test_scan_archive passed")
    finally:
        os.environ.pop("STOCK_QUANT_DB", None)
        path.unlink(missing_ok=True)


if __name__ == "__main__":
    main()
