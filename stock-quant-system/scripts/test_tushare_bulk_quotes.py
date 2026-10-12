"""Tushare 按日批量行情（ingestion.tushare_market_data §bulk quotes）合成数据测试，不联网、不碰主库。

闸门 4：验证 (1) 单位/列对齐新浪库口径；(2) adj 跳变票的历史行重定基；(3) 多日顺序补数后
全部行满足 qfq == raw·adj(t)/adj_latest；(4) 基准审计能抓出“旧基准”票且容忍半分钱舍入；
(5) 端到端 sync_bulk_quotes_by_date（fetch 函数打桩）在临时 DuckDB 上的行为。
"""
from __future__ import annotations

import datetime as dt
import os
import sys
import tempfile
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT))

_TMP = tempfile.TemporaryDirectory()
os.environ["STOCK_QUANT_DB"] = str(Path(_TMP.name) / "bulk_test.duckdb")

import pandas as pd  # noqa: E402

from common.db import init_schema, write_session  # noqa: E402
from common.http_retry import IntervalLimiter  # noqa: E402
from common.tushare_client import upsert  # noqa: E402
from ingestion import tushare_market_data as tmd  # noqa: E402

D = lambda s: pd.to_datetime(s, format="%Y%m%d").date()  # noqa: E731


def _dates(df: pd.DataFrame) -> pd.DataFrame:
    """DuckDB DATE 列读成 datetime64；统一成 python date 便于与 D() 比较。"""
    if "trade_date" in df.columns:
        df = df.copy()
        df["trade_date"] = pd.to_datetime(df["trade_date"]).dt.date
    return df


def _daily(trade_date: str, rows: list[tuple[str, float, float]]) -> pd.DataFrame:
    """rows: (ts_code, close, vol_in_hands)。open/high/low 围绕 close 构造。"""
    return pd.DataFrame(
        {
            "ts_code": [r[0] for r in rows],
            "trade_date": trade_date,
            "open": [r[1] * 0.99 for r in rows],
            "high": [r[1] * 1.02 for r in rows],
            "low": [r[1] * 0.98 for r in rows],
            "close": [r[1] for r in rows],
            "pre_close": [r[1] for r in rows],
            "change": 0.0,
            "pct_chg": 1.5,
            "vol": [r[2] for r in rows],
            "amount": [r[1] * r[2] / 10.0 for r in rows],  # 千元
        }
    )


def _adj(trade_date: str, rows: list[tuple[str, float]]) -> pd.DataFrame:
    return pd.DataFrame({"ts_code": [r[0] for r in rows], "trade_date": trade_date, "adj_factor": [r[1] for r in rows]})


def _turn(trade_date: str, rows: list[tuple[str, float]]) -> pd.DataFrame:
    return pd.DataFrame({"ts_code": [r[0] for r in rows], "trade_date": trade_date, "turnover_rate": [r[1] for r in rows]})


def test_normalize_bulk_daily_units_and_columns() -> None:
    raw = _daily("20261009", [("000001.SZ", 11.59, 1078105.61), ("920000.BJ", 14.0, 1000.0), ("999999.SH", 1.0, 1.0)])
    turn = _turn("20261009", [("000001.SZ", 0.5556)])
    out = tmd.normalize_bulk_daily(raw, turn, universe={"000001", "920000"})
    assert list(out.columns) == tmd.BULK_QUOTES_COLS
    assert set(out["symbol"]) == {"000001", "920000"}, "universe 过滤失效"
    r = out.set_index("symbol").loc["000001"]
    assert r["trade_date"] == D("20261009")
    assert abs(r["volume"] - 107810561.0) < 1e-6, "vol 手→股 ×100"
    assert abs(r["amount"] - 11.59 * 1078105.61 / 10.0 * 1000.0) < 1e-3, "amount 千元→元 ×1000"
    assert abs(r["turnover"] - 0.005556) < 1e-12, "turnover_rate % → 小数"
    assert abs(r["close"] - 11.59) < 1e-12 and r["adjust"] == "qfq"
    assert abs(r["pct_change"] - 1.5) < 1e-12
    assert pd.isna(out.set_index("symbol").loc["920000", "turnover"]), "无换手率的票应为空"
    assert tmd.normalize_bulk_daily(pd.DataFrame()).empty
    assert tmd.normalize_bulk_daily(raw, None).turnover.isna().all()


def test_normalize_bulk_adj() -> None:
    out = tmd.normalize_bulk_adj(_adj("20261009", [("000001.SZ", 142.0699), ("000001.SZ", 142.0699), ("600000.SH", None)]))
    assert list(out.columns) == ["symbol", "trade_date", "adj_factor"]
    assert len(out) == 1 and out.iloc[0]["symbol"] == "000001" and out.iloc[0]["trade_date"] == D("20261009")


def test_compute_readjust_ratios_only_changed() -> None:
    prev = tmd.normalize_bulk_adj(_adj("20260923", [("A.SZ", 1.0), ("B.SZ", 2.0), ("C.SZ", 1.5), ("D.SZ", 3.0)]))
    today = tmd.normalize_bulk_adj(_adj("20260924", [("A.SZ", 1.0), ("B.SZ", 3.0), ("C.SZ", 1.5000000000001), ("E.SZ", 9.0)]))
    r = tmd.compute_readjust_ratios(prev, today)
    assert list(r["symbol"]) == ["B"], f"只应有 B 跳变: {r}"
    assert abs(r.iloc[0]["ratio"] - 2.0 / 3.0) < 1e-12
    assert tmd.compute_readjust_ratios(None, today).empty
    assert tmd.compute_readjust_ratios(prev, pd.DataFrame(columns=["symbol", "trade_date", "adj_factor"])).empty


def _reset_db() -> None:
    with write_session(init=True) as conn:
        for t in ("daily_quotes", "adj_factor", "universe", "trade_calendar", "sync_log"):
            conn.execute(f"DELETE FROM {t}")


def _seed_universe(conn, symbols: dict[str, str]) -> None:
    conn.register(
        "u_in",
        pd.DataFrame(
            {
                "symbol": list(symbols),
                "exchange": list(symbols.values()),
                "board": ["bse" if e == "bj" else "main" for e in symbols.values()],
                "name": list(symbols),
                "is_st": False,
                "is_delisted": False,
            }
        ),
    )
    conn.execute("INSERT INTO universe (symbol, exchange, board, name, is_st, is_delisted) SELECT symbol, exchange, board, name, is_st, is_delisted FROM u_in")
    conn.unregister("u_in")


def test_apply_readjust_scales_only_history_ohlc() -> None:
    _reset_db()
    with write_session() as conn:
        rows = tmd.normalize_bulk_daily(_daily("20260922", [("B.SZ", 30.0, 100.0), ("A.SZ", 10.0, 50.0)]))
        rows2 = tmd.normalize_bulk_daily(_daily("20260923", [("B.SZ", 20.0, 100.0), ("A.SZ", 10.0, 50.0)]))
        upsert(conn, "daily_quotes", ["symbol", "trade_date", "adjust"], pd.concat([rows, rows2]))
        n = tmd.apply_readjust(conn, pd.DataFrame({"symbol": ["B"], "ratio": [2.0 / 3.0]}), D("20260923"))
        assert n == 1
        got = _dates(conn.execute("SELECT symbol, trade_date, open, close, volume FROM daily_quotes ORDER BY symbol, trade_date").df())
    g = got.set_index(["symbol", "trade_date"])
    assert abs(g.loc[("B", D("20260922")), "close"] - 20.0) < 1e-9, "历史行应重定基"
    assert abs(g.loc[("B", D("20260922")), "open"] - 30.0 * 0.99 * 2 / 3) < 1e-9
    assert abs(g.loc[("B", D("20260922")), "volume"] - 10000.0) < 1e-9, "volume 不应变"
    assert abs(g.loc[("B", D("20260923")), "close"] - 20.0) < 1e-9, "当日行不应变"
    assert abs(g.loc[("A", D("20260922")), "close"] - 10.0) < 1e-9, "未跳变票不应变"
    assert tmd.apply_readjust(None, pd.DataFrame(columns=["symbol", "ratio"]), D("20260923")) == 0


def _simulate_days(days: list[tuple[str, dict[str, tuple[float, float]]]]) -> None:
    """days: [(YYYYMMDD, {ts_code: (raw_close, adj_factor)})]，按 sync 的顺序逻辑写临时库。"""
    adj_prev = None
    for d, m in days:
        daily = _daily(d, [(k, v[0], 100.0) for k, v in m.items()])
        adj_today = tmd.normalize_bulk_adj(_adj(d, [(k, v[1]) for k, v in m.items()]))
        rows = tmd.normalize_bulk_daily(daily)
        ratios = tmd.compute_readjust_ratios(adj_prev, adj_today)
        with write_session() as conn:
            tmd.apply_readjust(conn, ratios, D(d))
            upsert(conn, "daily_quotes", ["symbol", "trade_date", "adjust"], rows)
            upsert(conn, "adj_factor", ["symbol", "trade_date"], adj_today)
        adj_prev = adj_today


def test_sequential_days_invariant_qfq_equals_raw_adj_over_latest() -> None:
    """三日顺序补数，X 在第 2 日 10 送 10（adj 1.0→2.0）、第 3 日再派息（2.0→2.1）；Y 无事件。"""
    _reset_db()
    days = [
        ("20260921", {"X.SZ": (40.0, 1.0), "Y.SH": (10.0, 5.0)}),
        ("20260922", {"X.SZ": (21.0, 2.0), "Y.SH": (10.5, 5.0)}),
        ("20260923", {"X.SZ": (20.5, 2.1), "Y.SH": (10.2, 5.0)}),
    ]
    _simulate_days(days)
    latest = {"X": 2.1, "Y": 5.0}
    with write_session() as conn:
        got = _dates(conn.execute("SELECT symbol, trade_date, close FROM daily_quotes WHERE adjust='qfq'").df())
    for _, r in got.iterrows():
        raw, adj = next(m[f"{r.symbol}.SZ" if r.symbol == "X" else f"{r.symbol}.SH"] for d, m in days if D(d) == r.trade_date)
        expected = raw * adj / latest[r.symbol]
        assert abs(r.close / expected - 1) < 1e-12, f"{r.symbol} {r.trade_date}: {r.close} vs {expected}"
    x = got[got.symbol == "X"].sort_values("trade_date")["close"].tolist()
    assert abs(x[0] - 40.0 * 1.0 / 2.1) < 1e-12 and abs(x[2] - 20.5) < 1e-12, "最新日 qfq == 未复权价"


def test_audit_flags_stale_basis_and_tolerates_rounding() -> None:
    # 库内：S 停在旧基准（除权前抓的，没重定基），OK 一致，R 只差半分钱舍入
    stored = pd.DataFrame({"symbol": ["S", "OK", "R"], "close": [18.05, 10.0, 3.33]})
    daily_t = _daily("20260724", [("S.SZ", 18.05, 1.0), ("OK.SZ", 10.0, 1.0), ("R.SZ", 3.33 * 1.5 / 1.5, 1.0)])
    adj_t = _adj("20260724", [("S.SZ", 1.1143), ("OK.SZ", 2.0), ("R.SZ", 1.0)])
    latest = tmd.normalize_bulk_adj(_adj("20261009", [("S.SZ", 1.6726), ("OK.SZ", 2.0), ("R.SZ", 1.0)]))
    daily_t.loc[daily_t.ts_code == "R.SZ", "close"] = 3.334  # 期望 3.334，库内 3.33 → 0.12% 但 < 半分钱
    bad = tmd.audit_qfq_basis(stored, daily_t, adj_t, latest, rel_tol=0.001)
    assert list(bad["symbol"]) == ["S"], f"应只标记 S: {bad}"
    assert abs(bad.iloc[0]["rel"] - (1.1143 / 1.6726 - 1)) < 1e-9
    assert tmd.audit_qfq_basis(pd.DataFrame(), daily_t, adj_t, latest).empty


def test_pick_audit_dates() -> None:
    dates = [f"2026{m:02d}{d:02d}" for m in range(1, 13) for d in range(1, 29)]
    picked = tmd.pick_audit_dates(dates, 24)
    assert picked == sorted(set(picked)) and picked[-1] == dates[-1]
    assert dates[-5] in picked and dates[-20] in picked and dates[-60] in picked and dates[0] in picked
    assert len(picked) <= 24 + 4
    assert tmd.pick_audit_dates([], 5) == []
    assert tmd.pick_audit_dates(["20260101"], 5) == ["20260101"]


def test_bulk_pending_dates_uses_coverage() -> None:
    _reset_db()
    today = D("20260925")
    with write_session() as conn:
        _seed_universe(conn, {"A": "sz", "B": "sz", "C": "sh", "D": "sh", "E": "bj"})
        conn.register("cal", pd.DataFrame({"trade_date": [D("20260922"), D("20260923"), D("20260924"), D("20260925"), D("20260928")]}))
        conn.execute("INSERT INTO trade_calendar SELECT trade_date FROM cal")
        conn.unregister("cal")
        # 09-22 五票全有；09-23 只有 1 票（quotes_decision 式局部新行）→ 有效日应为 09-22
        rows = tmd.normalize_bulk_daily(_daily("20260922", [(f"{s}.SZ", 1.0, 1.0) for s in "ABCDE"]))
        rows2 = tmd.normalize_bulk_daily(_daily("20260923", [("A.SZ", 1.0, 1.0)]))
        upsert(conn, "daily_quotes", ["symbol", "trade_date", "adjust"], pd.concat([rows, rows2]))
        eff, pending = tmd.bulk_pending_trade_dates(conn, 0.8, today)
    assert eff == D("20260922"), eff
    assert pending == ["20260923", "20260924", "20260925"], pending


def test_sync_bulk_quotes_end_to_end_stubbed(monkey_calls: dict | None = None) -> None:
    """fetch 打桩：有效日 09-22（库内新浪式旧行），补 09-23/09-24；B 在 09-24 除权；09-25 Tushare 还没发布。"""
    _reset_db()
    today = D("20260925")
    with write_session() as conn:
        _seed_universe(conn, {"A": "sz", "B": "sz", "C": "sh", "E": "bj"})
        conn.register("cal", pd.DataFrame({"trade_date": [D("20260922"), D("20260923"), D("20260924"), D("20260925")]}))
        conn.execute("INSERT INTO trade_calendar SELECT trade_date FROM cal")
        conn.unregister("cal")
        # 库内 09-22 四票（基准 = 09-22 adj），其中 B 的旧基准 adj=1.0；E(bj) 当日也在
        seed = tmd.normalize_bulk_daily(_daily("20260922", [("A.SZ", 10.0, 1), ("B.SZ", 30.0, 1), ("C.SH", 5.0, 1), ("E.BJ", 8.0, 1)]))
        upsert(conn, "daily_quotes", ["symbol", "trade_date", "adjust"], seed)

    world = {
        "20260922": ({"A.SZ": (10.0, 1.0), "B.SZ": (30.0, 1.0), "C.SH": (5.0, 1.0), "E.BJ": (8.0, 1.0)}),
        "20260923": ({"A.SZ": (10.1, 1.0), "B.SZ": (31.0, 1.0), "C.SH": (5.1, 1.0), "E.BJ": (8.1, 1.0)}),
        "20260924": ({"A.SZ": (10.2, 1.0), "B.SZ": (15.6, 2.0), "C.SH": (5.2, 1.0), "E.BJ": (8.2, 1.0)}),
    }
    calls = {"daily": [], "adj": [], "turn": []}

    def fake_daily(d: str) -> pd.DataFrame:
        calls["daily"].append(d)
        if d not in world:
            return pd.DataFrame()
        return _daily(d, [(k, v[0], 100.0) for k, v in world[d].items()])

    def fake_adj(d: str) -> pd.DataFrame:
        calls["adj"].append(d)
        return _adj(d, [(k, v[1]) for k, v in world[d].items()]) if d in world else pd.DataFrame()

    def fake_turn(d: str) -> pd.DataFrame:
        calls["turn"].append(d)
        return _turn(d, [(k, 1.0) for k in world[d]])

    orig = (tmd._fetch_bulk_daily, tmd._fetch_bulk_adj_factor, tmd._fetch_bulk_turnover, tmd.tushare_limiter, dt.date)
    tmd._fetch_bulk_daily, tmd._fetch_bulk_adj_factor, tmd._fetch_bulk_turnover = fake_daily, fake_adj, fake_turn
    tmd.tushare_limiter = lambda: IntervalLimiter(0.0, 0.0)

    class _FakeDate(dt.date):
        @classmethod
        def today(cls):
            return today

    tmd.dt.date = _FakeDate  # type: ignore[misc]
    try:
        stats = tmd.sync_bulk_quotes_by_date(audit=False, rebuild=False)
    finally:
        tmd._fetch_bulk_daily, tmd._fetch_bulk_adj_factor, tmd._fetch_bulk_turnover, tmd.tushare_limiter, tmd.dt.date = orig  # type: ignore[misc]

    assert stats["effective_date"] == "2026-09-22", stats
    assert stats["dates_pending"] == 3 and stats["dates_written"] == 2 and stats["dates_empty"] == 1, stats
    assert stats["rows_written"] == 8 and stats["readjusted_symbols"] == 1, stats
    assert stats["readjusted_rows"] == 2, "B 的 09-22 与 09-23 两行应重定基"
    assert calls["adj"][0] == "20260922", "应先取有效日 adj 作为旧基准"
    assert calls["daily"] == ["20260923", "20260924", "20260925"]
    with write_session() as conn:
        got = _dates(conn.execute("SELECT symbol, trade_date, close, turnover, volume FROM daily_quotes WHERE adjust='qfq' ORDER BY symbol, trade_date").df())
        adj_rows = conn.execute("SELECT count(*) FROM adj_factor").fetchone()[0]
        logs = conn.execute("SELECT count(*) FROM sync_log WHERE task_name='sync_bulk_quotes_by_date'").fetchone()[0]
    g = got.set_index(["symbol", "trade_date"])["close"]
    assert abs(g[("B", D("20260922"))] - 15.0) < 1e-9 and abs(g[("B", D("20260923"))] - 15.5) < 1e-9, "B 历史重定基到最新基准"
    assert abs(g[("B", D("20260924"))] - 15.6) < 1e-9
    assert abs(g[("A", D("20260922"))] - 10.0) < 1e-9 and abs(g[("E", D("20260924"))] - 8.2) < 1e-9, "bj 也被填"
    assert abs(got.set_index(["symbol", "trade_date"]).loc[("A", D("20260923")), "turnover"] - 0.01) < 1e-12
    assert adj_rows == 8 and logs == 3, (adj_rows, logs)


def main() -> None:
    test_normalize_bulk_daily_units_and_columns()
    test_normalize_bulk_adj()
    test_compute_readjust_ratios_only_changed()
    test_apply_readjust_scales_only_history_ohlc()
    test_sequential_days_invariant_qfq_equals_raw_adj_over_latest()
    test_audit_flags_stale_basis_and_tolerates_rounding()
    test_pick_audit_dates()
    test_bulk_pending_dates_uses_coverage()
    test_sync_bulk_quotes_end_to_end_stubbed()
    print("test_tushare_bulk_quotes: all passed")


if __name__ == "__main__":
    try:
        main()
    finally:
        _TMP.cleanup()
