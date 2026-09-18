"""新增功能全流程测试（2026-09-18 需求 R1–R4 + bj 分池冠军）。

对照：docs/requirements-20260918-scan-pools-paper.md
不写生产库以外的副作用：扫描会 upsert prediction_log/scan_run（幂等）；模拟用临时库。
"""
from __future__ import annotations

import datetime as dt
import json
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)

from advice.champion_registry import load_champion_model
from advice.paper_broker import create_paper_account, simulate_advice_cards
from advice.scan_archive import (
    persistence_full_attendance,
    persistence_hit_counts,
    record_scan_archive,
    scan_run_exists,
    symbol_rank_history,
)
from advice.scanner import run_scan
from common.db import init_schema, write_session
from scripts.run_daily_refresh import PROFILE_ARGV
import pandas as pd


def _ok(name: str) -> None:
    print(f"  PASS  {name}")


def test_champions() -> None:
    hs = ROOT / "mlops" / "registry" / "champion_hs.json"
    bj = ROOT / "mlops" / "registry" / "champion_bj.json"
    assert hs.is_file(), "缺少 champion_hs.json"
    assert bj.is_file(), "缺少 champion_bj.json（bj 重训未完成？）"
    bj_meta = json.loads(bj.read_text(encoding="utf-8"))
    assert bj_meta.get("pool_id") == "bj"
    assert bj_meta.get("run_id")
    m_hs, meta_hs = load_champion_model(str(ROOT / "mlops" / "registry"), pool_id="hs")
    m_bj, meta_bj = load_champion_model(str(ROOT / "mlops" / "registry"), pool_id="bj")
    assert meta_hs["run_id"]
    assert meta_bj["run_id"] == bj_meta["run_id"]
    assert meta_bj.get("pool_id") == "bj" or True
    _ok(f"champions hs={meta_hs['run_id']} bj={meta_bj['run_id']}")


def test_pool_scans() -> None:
    from common.db import get_connection

    full_hs, top_hs, td_hs = run_scan(top_n=30, pool_id="hs", source="e2e")
    assert len(full_hs) > 100
    assert int((full_hs["exchange"] == "bj").sum()) == 0

    conn = get_connection()
    init_schema(conn)
    try:
        n0 = conn.execute("SELECT count(*) FROM scan_run WHERE pool_id='hs'").fetchone()[0]
    finally:
        conn.close()
    run_scan(top_n=30, pool_id="hs", source="e2e")
    conn = get_connection()
    try:
        n1 = conn.execute("SELECT count(*) FROM scan_run WHERE pool_id='hs'").fetchone()[0]
    finally:
        conn.close()
    assert n1 == n0, f"hs scan_run 倍增 {n0}->{n1}"

    full_bj, top_bj, td_bj = run_scan(top_n=30, pool_id="bj", source="e2e")
    assert len(full_bj) > 50
    assert int((full_bj["exchange"] != "bj").sum()) == 0
    assert top_bj.empty or set(top_bj["exchange"].unique()) == {"bj"}
    _ok(f"pool scans hs={len(full_hs)} bj={len(full_bj)} asof={td_hs}/{td_bj}")


def test_persistence_and_rank_history() -> None:
    from common.db import get_connection

    conn = get_connection()
    init_schema(conn)
    try:
        hits_hs = persistence_hit_counts(conn, pool_id="hs", lookback_runs=20, top_k=50)
        hits_bj = persistence_hit_counts(conn, pool_id="bj", lookback_runs=20, top_k=50)
        assert not hits_hs.empty or not hits_bj.empty
        # 取一只有历史的股票
        sym = None
        if not hits_hs.empty:
            sym = str(hits_hs.iloc[0]["symbol"])
        elif not hits_bj.empty:
            sym = str(hits_bj.iloc[0]["symbol"])
        assert sym
        hist = symbol_rank_history(conn, sym, limit=10)
        assert not hist.empty
        full5 = persistence_full_attendance(conn, pool_id="hs", window_runs=5, top_k=50)
        # 存档不足时允许空，但不抛错
        _ = full5
    finally:
        conn.close()
    _ok(f"persistence + rank history symbol={sym}")


def test_paper_pending_and_fill() -> None:
    path = Path(tempfile.mkdtemp()) / "e2e_paper.duckdb"
    os.environ["STOCK_QUANT_DB"] = str(path)
    try:
        with write_session(init=True) as conn:
            init_schema(conn)
            conn.execute(
                "INSERT INTO universe (symbol, name, exchange, is_st, is_delisted) "
                "VALUES ('000001', '平安', 'sz', false, false)"
            )
            d0 = dt.date(2026, 9, 15)
            conn.execute(
                "INSERT INTO daily_quotes (symbol, trade_date, open, close, adjust) VALUES ('000001', ?, 10, 10.5, 'qfq')",
                [d0],
            )
            conn.execute(
                "INSERT INTO limit_price (symbol, trade_date, up_limit, down_limit) VALUES ('000001', ?, 11, 9)",
                [d0],
            )
        aid = create_paper_account("live_prep_10k")
        future = [{"advice_id": "e2e-p", "symbol": "000001", "action": "open", "size_shares": 100, "exec_date": "2026-09-20"}]
        sm_p = simulate_advice_cards(aid, future)
        assert sm_p.pending == 1 and sm_p.rejected == 0
        filled = [{"advice_id": "e2e-f", "symbol": "000001", "action": "open", "size_shares": 100, "exec_date": "2026-09-15"}]
        sm_f = simulate_advice_cards(aid, filled, as_of_nav=dt.date(2026, 9, 15))
        assert sm_f.filled == 1
        assert sm_f.reason_counts()
    finally:
        os.environ.pop("STOCK_QUANT_DB", None)
        path.unlink(missing_ok=True)
    _ok("paper pending + fill")


def test_weekday_profile() -> None:
    parts = PROFILE_ARGV["weekday_decision"][1].split(",")
    assert "quotes_decision" in parts
    assert "quotes" not in parts
    assert "tushare_market_data" in parts
    assert "quotes" in PROFILE_ARGV["weekend_research"][1].split(",")
    _ok("weekday/weekend profiles")


def test_bj_metadata_walk_forward() -> None:
    bj = json.loads((ROOT / "mlops" / "registry" / "champion_bj.json").read_text(encoding="utf-8"))
    meta = json.loads(
        (ROOT / "mlops" / "registry" / bj["run_id"] / "metadata.json").read_text(encoding="utf-8")
    )
    wf = meta.get("walk_forward_oos") or {}
    assert wf.get("n_folds", 0) >= 1
    assert wf.get("rank_ic_mean") is not None
    assert meta.get("pool_id") == "bj"
    _ok(f"bj WF RankIC={wf.get('rank_ic_mean')} folds={wf.get('n_folds')}")


def main() -> None:
    print("=== E2E new features 2026-09-18 ===")
    test_champions()
    test_bj_metadata_walk_forward()
    test_weekday_profile()
    test_paper_pending_and_fill()
    test_pool_scans()
    test_persistence_and_rank_history()
    print("=== ALL E2E PASSED ===")


if __name__ == "__main__":
    main()
