"""披露日历抓取修复回归（不碰主库）。闸门 4。"""
from __future__ import annotations

import os
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT))

from ingestion.reference_data import _fetch_disclosure_period, sync_disclosure_calendar


def test_fetch_one_period_cninfo_or_tushare() -> None:
    import datetime as dt

    raw, source = _fetch_disclosure_period("沪深京", "2025半年报", dt.date(2025, 6, 30))
    assert source in ("cninfo", "tushare")
    assert len(raw) > 100


def test_sync_one_year_temp_db() -> None:
    db = _ROOT / "data" / "test_disclosure_fix.duckdb"
    if db.exists():
        db.unlink()
    os.environ["STOCK_QUANT_DB"] = str(db)
    stats = sync_disclosure_calendar(start_year=2025, end_year=2025)
    assert stats["periods_done"] >= 1
    assert stats["rows"] > 0
    assert not stats.get("aborted_early")
    db.unlink()


def main() -> None:
    test_fetch_one_period_cninfo_or_tushare()
    test_sync_one_year_temp_db()
    print("test_reference_disclosure: all passed")


if __name__ == "__main__":
    main()
