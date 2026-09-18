"""回归：北交所 qfq 转换在「旧 adj 与增量日线无交集」时必须失败；刷新 adj 覆盖缺口后成功。

对应 2026-09-18 事故：库内旧 adj_factor 非空 → 跳过刷新 → 增量日线与旧 adj 无共同
trade_date（left merge 丢弃更早因子行）→「复权因子无法对齐日线」→ rows_written=0。
不连网、不写生产库。
"""
from __future__ import annotations

import inspect
import sys
from datetime import date
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from ingestion.tushare_prices import _raw_daily_to_qfq, sync_bse_cdr_qfq_quotes


def test_gap_daily_without_overlapping_adj_yields_empty() -> None:
    """增量日线与库内旧 adj 无共同 trade_date 时，left merge 后全 NaN（生产事故路径）。"""
    daily = pd.DataFrame(
        {
            "trade_date": ["20260911", "20260912", "20260917"],
            "open": [10.0, 10.1, 10.2],
            "high": [10.5, 10.6, 10.7],
            "low": [9.5, 9.6, 9.7],
            "close": [10.2, 10.3, 10.4],
            "vol": [1000.0, 1100.0, 1200.0],
            "amount": [100.0, 110.0, 120.0],
            "pct_chg": [1.0, 1.0, 1.0],
        }
    )
    # 库内旧因子只到缺口起点之前，与增量日线无交集 → 无法对齐
    adj = pd.DataFrame(
        {
            "trade_date": [date(2026, 9, 1), date(2026, 9, 10)],
            "adj_factor": [1.0, 1.05],
        }
    )
    out = _raw_daily_to_qfq(daily, adj, "920165")
    assert out.empty, "旧 adj 与增量日线无交集时应跳过（须先刷新 adj）"


def test_refreshed_adj_covering_gap_writes_qfq() -> None:
    daily = pd.DataFrame(
        {
            "trade_date": ["20260911", "20260912", "20260917"],
            "open": [10.0, 10.1, 10.2],
            "high": [10.5, 10.6, 10.7],
            "low": [9.5, 9.6, 9.7],
            "close": [10.2, 10.3, 10.4],
            "vol": [1000.0, 1100.0, 1200.0],
            "amount": [100.0, 110.0, 120.0],
            "pct_chg": [1.0, 1.0, 1.0],
        }
    )
    adj = pd.DataFrame(
        {
            "trade_date": [date(2026, 9, 11), date(2026, 9, 12), date(2026, 9, 17)],
            "adj_factor": [1.05, 1.05, 1.06],
        }
    )
    out = _raw_daily_to_qfq(daily, adj, "920165")
    assert not out.empty
    assert len(out) == 3
    assert set(out["adjust"].unique()) == {"qfq"}
    assert float(out["volume"].iloc[0]) == 1000.0 * 100


def test_sync_bse_defaults_refresh_adj() -> None:
    sig = inspect.signature(sync_bse_cdr_qfq_quotes)
    assert sig.parameters["refresh_adj"].default is True


def main() -> None:
    test_gap_daily_without_overlapping_adj_yields_empty()
    test_refreshed_adj_covering_gap_writes_qfq()
    test_sync_bse_defaults_refresh_adj()
    from common.ingestion_engine.orchestrator import STEP_FUNCS, STEP_ORDER
    from scripts.run_daily_refresh import PROFILE_ARGV

    assert "tushare_bse_quotes" in STEP_ORDER
    assert "tushare_bse_quotes" in STEP_FUNCS
    assert "quotes_decision" in STEP_ORDER
    assert "quotes_decision" in STEP_FUNCS
    # 日更：持仓增量，不再默认全市场 bj 轻量步
    wd = PROFILE_ARGV["weekday_decision"][1]
    assert "quotes_decision" in wd
    assert "tushare_bse_quotes" not in wd.split(",")
    assert "tushare_bse_quotes" in PROFILE_ARGV["bootstrap"][PROFILE_ARGV["bootstrap"].index("--skip") + 1]
    print("OK: bse adj refresh regressions passed")


if __name__ == "__main__":
    main()
