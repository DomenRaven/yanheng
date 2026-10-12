"""
Tushare Pro 集成 —— 每日市值指标 + 指数历史成分(point-in-time) + 停复牌/官方涨跌停价格
+ 按交易日全市场日线（daily + adj_factor → qfq，见 §bulk quotes）。

daily_basic 默认按 trade_date 拉全市场快照（~2600 次/全历史），替代逐股 ~5500 次；
步内子任务断点见 common.ingestion_engine.pipeline_state.substeps。

§bulk quotes（2026-10-12，docs/probe-tushare-bulk-quotes-20261012.md）
    sync_bulk_quotes_by_date：每个交易日 3 次调用（daily / adj_factor / daily_basic）填满
    sh+sz+bj 全市场 qfq 行情，替代 5224 次逐股新浪 HTTP（周末链 quotes 步 52~65 分钟 → 分钟级）。
    qfq 口径与库内新浪 qfq 一致：以「最新 adj_factor」为基准，最新日 qfq == 未复权价；
    某票 adj_factor 在 D 日跳变（除权除息）时，把该票 D 之前的历史行乘以 adj(prev)/adj(D)
    整体重定基，再写 D 日行——这是增量 qfq 存储正确的唯一方式（新浪逐股增量没做这步，
    探针实测 508 只票 /100 万行停留在旧基准）。
    开关：config.yaml -> ingestion.tushare_bulk_quotes.enabled（orchestrator quotes 步读取）。
"""
from __future__ import annotations

import datetime as dt
import logging
from typing import Any

import pandas as pd

from common.config import get_config
from common.db import get_connection, init_schema, write_session
from common.http_retry import retry_on_failure, tushare_limiter
from common.ingestion_engine.pipeline_state import load_state, mark_substep, substep_is_ok
from common.tushare_client import (
    get_pro_api,
    get_trade_dates,
    run_date_loop_sync,
    run_item_loop_sync,
    to_ts_code,
    upsert,
)

logger = logging.getLogger("ingestion.tushare_market_data")

PIPELINE_STEP = "tushare_market_data"
MARKET_SUBTASKS = ("daily_basic", "index_weight", "suspend_calendar", "limit_price")

_CONSTITUENT_INDICES = {"000300.SH": "沪深300", "000905.SH": "中证500", "000852.SH": "中证1000"}

_DAILY_BASIC_COLS = [
    "symbol",
    "trade_date",
    "close",
    "turnover_rate",
    "turnover_rate_f",
    "volume_ratio",
    "pe",
    "pe_ttm",
    "pb",
    "ps",
    "ps_ttm",
    "dv_ratio",
    "dv_ttm",
    "total_share",
    "float_share",
    "free_share",
    "total_mv",
    "circ_mv",
]


def _pipeline_running() -> bool:
    state = load_state()
    return bool(state and state.get("status") == "running")


def _skip_market_subtask(name: str) -> bool:
    return substep_is_ok(load_state(), PIPELINE_STEP, name)


def _record_market_subtask(name: str, stats: dict[str, Any]) -> None:
    state = load_state()
    if state and state.get("status") == "running":
        mark_substep(state, PIPELINE_STEP, name, {"status": "ok", "stats": stats})


def _active_universe_symbols(conn) -> set[str]:
    rows = conn.execute(
        "SELECT symbol FROM universe WHERE exchange IN ('sh','sz','bj') AND is_delisted = FALSE"
    ).fetchall()
    return {r[0] for r in rows}


def _daily_basic_trade_dates(conn, limit: int | None) -> list[str]:
    cfg = get_config()
    start = cfg["ingestion"]["history_start_date"]
    end = dt.date.today().strftime("%Y%m%d")
    dates = get_trade_dates(conn, start, end)
    row = conn.execute("SELECT max(trade_date) FROM daily_basic").fetchone()
    max_d = row[0] if row else None
    if max_d is not None:
        max_s = max_d.strftime("%Y%m%d") if hasattr(max_d, "strftime") else str(max_d).replace("-", "")[:8]
        dates = [d for d in dates if d > max_s]
    if limit:
        dates = dates[:limit]
    return dates


def _fetch_daily_basic_by_date(trade_date: str) -> pd.DataFrame:
    return get_pro_api().daily_basic(trade_date=trade_date)


def _normalize_daily_basic_snapshot(df: pd.DataFrame, universe: set[str]) -> pd.DataFrame:
    if df.empty:
        return df
    out = df.copy()
    if "ts_code" in out.columns:
        out["symbol"] = out["ts_code"].astype(str).str.split(".").str[0]
    elif "symbol" not in out.columns:
        return pd.DataFrame()
    out["trade_date"] = pd.to_datetime(out["trade_date"], format="%Y%m%d", errors="coerce").dt.date
    out = out.dropna(subset=["trade_date"])
    out = out[out["symbol"].isin(universe)]
    for c in _DAILY_BASIC_COLS:
        if c not in out.columns:
            out[c] = None
    return out[_DAILY_BASIC_COLS].drop_duplicates(subset=["symbol", "trade_date"])


def sync_daily_basic(symbols: list[str] | None = None, limit: int | None = None) -> dict:
    if symbols:
        logger.warning("daily_basic 已改为按 trade_date 快照；symbols 参数忽略")
    conn = get_connection()
    init_schema(conn)
    universe = _active_universe_symbols(conn)
    trade_dates = _daily_basic_trade_dates(conn, limit)
    conn.close()
    if not trade_dates:
        logger.info("sync_daily_basic: 无待补交易日（已是最新）")
        return {"ok": 0, "empty": 0, "failed": 0, "rows_written": 0, "failed_dates": [], "skipped": True}

    uni = universe

    def normalize(df: pd.DataFrame) -> pd.DataFrame:
        return _normalize_daily_basic_snapshot(df, uni)

    return run_date_loop_sync(
        task_name="sync_daily_basic",
        table="daily_basic",
        key_cols=["symbol", "trade_date"],
        fetch_fn=_fetch_daily_basic_by_date,
        normalize_fn=normalize,
        trade_dates=trade_dates,
        limit=None,
    )


def _month_end_trade_dates(trade_dates: list[str]) -> list[str]:
    df = pd.DataFrame({"d": pd.to_datetime(trade_dates, format="%Y%m%d")})
    df["ym"] = df["d"].dt.to_period("M")
    last_per_month = df.groupby("ym")["d"].max()
    return [d.strftime("%Y%m%d") for d in last_per_month]


def _fetch_index_weight(item: tuple[str, str]) -> pd.DataFrame:
    index_code, trade_date = item
    return get_pro_api().index_weight(index_code=index_code, trade_date=trade_date)


def _normalize_index_weight(df: pd.DataFrame, item: tuple[str, str]) -> pd.DataFrame:
    index_code, _trade_date = item
    if df.empty:
        return df
    out = df.rename(columns={"con_code": "symbol"}).copy()
    out["symbol"] = out["symbol"].astype(str).str.split(".").str[0]
    out["trade_date"] = pd.to_datetime(out["trade_date"], format="%Y%m%d", errors="coerce").dt.date
    out = out.dropna(subset=["trade_date"])
    out["index_code"] = index_code
    return out[["index_code", "symbol", "trade_date", "weight"]]


def sync_index_weight(limit: int | None = None) -> dict:
    cfg = get_config()
    start = cfg["ingestion"]["history_start_date"]
    end = dt.date.today().strftime("%Y%m%d")
    conn = get_connection()
    init_schema(conn)
    trade_dates = get_trade_dates(conn, start, end)
    conn.close()
    month_ends = _month_end_trade_dates(trade_dates)
    items = [(index_code, td) for index_code in _CONSTITUENT_INDICES for td in month_ends]
    return run_item_loop_sync(
        task_name="sync_index_weight",
        table="index_weight",
        key_cols=["index_code", "symbol", "trade_date"],
        items=items,
        fetch_fn=_fetch_index_weight,
        normalize_fn=_normalize_index_weight,
        limit=limit,
        item_label=lambda x: f"{x[0]}@{x[1]}",
    )


def _fetch_suspend(trade_date: str) -> pd.DataFrame:
    return get_pro_api().suspend_d(trade_date=trade_date)


def _normalize_suspend(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty:
        return df
    out = df.rename(columns={"ts_code": "symbol"}).copy()
    out["symbol"] = out["symbol"].str.split(".").str[0]
    out["trade_date"] = pd.to_datetime(out["trade_date"], format="%Y%m%d", errors="coerce").dt.date
    out = out.dropna(subset=["trade_date"])
    return out[["symbol", "trade_date", "suspend_type"]].drop_duplicates(subset=["symbol", "trade_date"])


def sync_suspend_calendar(limit: int | None = None) -> dict:
    cfg = get_config()
    start = cfg["ingestion"]["history_start_date"]
    end = dt.date.today().strftime("%Y%m%d")
    conn = get_connection()
    init_schema(conn)
    trade_dates = get_trade_dates(conn, start, end)
    conn.close()
    return run_date_loop_sync(
        task_name="sync_suspend_calendar",
        table="suspend_calendar",
        key_cols=["symbol", "trade_date"],
        fetch_fn=_fetch_suspend,
        normalize_fn=_normalize_suspend,
        trade_dates=trade_dates,
        limit=limit,
    )


def _fetch_limit_price(trade_date: str) -> pd.DataFrame:
    return get_pro_api().stk_limit(trade_date=trade_date)


def _normalize_limit_price(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty:
        return df
    out = df.rename(columns={"ts_code": "symbol"}).copy()
    out["symbol"] = out["symbol"].str.split(".").str[0]
    out["trade_date"] = pd.to_datetime(out["trade_date"], format="%Y%m%d", errors="coerce").dt.date
    out = out.dropna(subset=["trade_date"])
    return out[["symbol", "trade_date", "up_limit", "down_limit"]].drop_duplicates(subset=["symbol", "trade_date"])


def sync_limit_price(limit: int | None = None) -> dict:
    cfg = get_config()
    start = cfg["ingestion"]["history_start_date"]
    end = dt.date.today().strftime("%Y%m%d")
    conn = get_connection()
    init_schema(conn)
    trade_dates = get_trade_dates(conn, start, end)
    conn.close()
    return run_date_loop_sync(
        task_name="sync_limit_price",
        table="limit_price",
        key_cols=["symbol", "trade_date"],
        fetch_fn=_fetch_limit_price,
        normalize_fn=_normalize_limit_price,
        trade_dates=trade_dates,
        limit=limit,
    )


# ---------------------------------------------------------------------------
# §bulk quotes：按交易日全市场 qfq 日线（daily + adj_factor + daily_basic.turnover_rate）
# ---------------------------------------------------------------------------

BULK_QUOTES_COLS = [
    "symbol", "trade_date", "open", "high", "low", "close",
    "volume", "amount", "turnover", "pct_change", "adjust",
]
_ADJ_CHANGE_TOL = 1e-9
# 新浪 qfq 收盘保留 2 位小数，比较时允许半分钱的舍入（探针：重定基后的新浪行偏差 ≤0.03%）
_HALF_FEN = 0.0051


def bulk_quotes_config() -> dict[str, Any]:
    cfg = get_config().get("ingestion", {}).get("tushare_bulk_quotes") or {}
    return {
        "enabled": bool(cfg.get("enabled", False)),
        "fallback_to_sina": bool(cfg.get("fallback_to_sina", True)),
        "coverage_ratio": float(cfg.get("coverage_ratio", 0.8)),
        "max_dates_per_run": int(cfg.get("max_dates_per_run", 60)),
        "audit_sample_dates": int(cfg.get("audit_sample_dates", 24)),
        "audit_rel_tol": float(cfg.get("audit_rel_tol", 0.001)),
        "max_rebuild_symbols": int(cfg.get("max_rebuild_symbols", 800)),
    }


def _fetch_bulk_daily(trade_date: str) -> pd.DataFrame:
    # 实测列：ts_code trade_date open high low close pre_close change pct_chg vol(手) amount(千元)
    return get_pro_api().daily(trade_date=trade_date)


def _fetch_bulk_adj_factor(trade_date: str) -> pd.DataFrame:
    # 实测列：ts_code trade_date adj_factor；含停牌票（行数 ≥ daily）
    return get_pro_api().adj_factor(trade_date=trade_date)


def _fetch_bulk_turnover(trade_date: str) -> pd.DataFrame:
    # daily_basic.turnover_rate 为 %；探针实测 == 新浪 turnover × 100（5184 票中 99.8% 在 ±1% 内）
    return get_pro_api().daily_basic(trade_date=trade_date, fields="ts_code,trade_date,turnover_rate")


def _split_symbol(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()
    if "ts_code" in out.columns:
        out["symbol"] = out["ts_code"].astype(str).str.split(".").str[0]
    return out


def normalize_bulk_adj(adj_raw: pd.DataFrame | None) -> pd.DataFrame:
    """adj_factor(trade_date=D) → [symbol, trade_date(date), adj_factor]。"""
    if adj_raw is None or adj_raw.empty:
        return pd.DataFrame(columns=["symbol", "trade_date", "adj_factor"])
    out = _split_symbol(adj_raw)
    out["trade_date"] = pd.to_datetime(out["trade_date"], format="%Y%m%d", errors="coerce").dt.date
    out["adj_factor"] = pd.to_numeric(out["adj_factor"], errors="coerce")
    out = out.dropna(subset=["symbol", "trade_date", "adj_factor"])
    return out[["symbol", "trade_date", "adj_factor"]].drop_duplicates(subset=["symbol"]).reset_index(drop=True)


def normalize_bulk_daily(
    daily_raw: pd.DataFrame | None,
    turnover: pd.DataFrame | None = None,
    universe: set[str] | None = None,
) -> pd.DataFrame:
    """daily(trade_date=D) → daily_quotes qfq 行。

    基准 = D 日 adj_factor（最新），比值恒为 1，所以 qfq 价格就是未复权价；历史行的重定基
    由 compute_readjust_ratios/apply_readjust 负责。单位对齐新浪库：vol 手→股(×100)、
    amount 千元→元(×1000)、turnover_rate %→小数(÷100)；pct_change 直接用 pct_chg(%)。
    """
    if daily_raw is None or daily_raw.empty:
        return pd.DataFrame(columns=BULK_QUOTES_COLS)
    out = _split_symbol(daily_raw)
    out["trade_date"] = pd.to_datetime(out["trade_date"], format="%Y%m%d", errors="coerce").dt.date
    for c in ("open", "high", "low", "close"):
        out[c] = pd.to_numeric(out[c], errors="coerce")
    out = out.dropna(subset=["symbol", "trade_date", "close"])
    if universe is not None:
        out = out[out["symbol"].isin(universe)]
    out["volume"] = pd.to_numeric(out["vol"], errors="coerce") * 100.0
    out["amount"] = pd.to_numeric(out["amount"], errors="coerce") * 1000.0
    out["pct_change"] = pd.to_numeric(out["pct_chg"], errors="coerce") if "pct_chg" in out.columns else None
    if turnover is not None and not turnover.empty and "turnover_rate" in turnover.columns:
        t = _split_symbol(turnover)[["symbol", "turnover_rate"]].drop_duplicates(subset=["symbol"])
        out = out.merge(t, on="symbol", how="left")
        out["turnover"] = pd.to_numeric(out["turnover_rate"], errors="coerce") / 100.0
    else:
        out["turnover"] = None
    out["adjust"] = "qfq"
    return out[BULK_QUOTES_COLS].drop_duplicates(subset=["symbol", "trade_date", "adjust"]).reset_index(drop=True)


def compute_readjust_ratios(
    adj_prev: pd.DataFrame | None, adj_today: pd.DataFrame, tol: float = _ADJ_CHANGE_TOL
) -> pd.DataFrame:
    """adj_factor 从上一基准日到 D 日发生跳变的票：ratio = adj_prev / adj_today。

    库内该票 D 之前的 qfq 行是按旧基准 adj_prev 定基的，整体乘以 ratio 后即与新基准一致：
    qfq_new(t) = raw(t)·adj(t)/adj_today = qfq_old(t)·adj_prev/adj_today。
    """
    if adj_prev is None or adj_prev.empty or adj_today.empty:
        return pd.DataFrame(columns=["symbol", "ratio"])
    m = adj_prev[["symbol", "adj_factor"]].merge(
        adj_today[["symbol", "adj_factor"]], on="symbol", suffixes=("_prev", "_today")
    )
    m = m[(m["adj_factor_prev"] > 0) & (m["adj_factor_today"] > 0)].copy()
    m["ratio"] = m["adj_factor_prev"] / m["adj_factor_today"]
    return m.loc[(m["ratio"] - 1).abs() > tol, ["symbol", "ratio"]].reset_index(drop=True)


def apply_readjust(conn, ratios: pd.DataFrame, before_date: dt.date) -> int:
    """把 ratios 中各票 before_date 之前的 qfq 行 OHLC 乘以 ratio；返回受影响行数。
    volume/amount/turnover/pct_change 与基准无关，不动。"""
    if ratios is None or ratios.empty:
        return 0
    conn.register("bulk_readjust", ratios[["symbol", "ratio"]])
    try:
        n = conn.execute(
            "SELECT count(*) FROM daily_quotes d JOIN bulk_readjust r USING(symbol) "
            "WHERE d.adjust = 'qfq' AND d.trade_date < ?",
            [before_date],
        ).fetchone()[0]
        conn.execute(
            """
            UPDATE daily_quotes
            SET open = open * r.ratio, high = high * r.ratio, low = low * r.ratio, close = close * r.ratio,
                updated_at = current_timestamp
            FROM bulk_readjust r
            WHERE daily_quotes.symbol = r.symbol AND daily_quotes.adjust = 'qfq' AND daily_quotes.trade_date < ?
            """,
            [before_date],
        )
    finally:
        conn.unregister("bulk_readjust")
    return int(n)


def bulk_pending_trade_dates(conn, coverage_ratio: float, today: dt.date | None = None) -> tuple[dt.date | None, list[str]]:
    """(有效截面日, 待补交易日列表 YYYYMMDD)。有效截面日 = 覆盖 ≥ coverage_ratio 活跃 universe 的最近 qfq 日
    （与 panel.build_asof_snapshot 同口径），避免被少数票的新行误判为“已最新”。"""
    today = today or dt.date.today()
    n_active = conn.execute(
        "SELECT count(*) FROM universe WHERE exchange IN ('sh','sz','bj') AND is_delisted = FALSE"
    ).fetchone()[0]
    row = conn.execute(
        """
        SELECT max(trade_date) FROM (
            SELECT trade_date, count(*) AS n FROM daily_quotes
            WHERE adjust = 'qfq' AND trade_date >= ? GROUP BY trade_date
        ) WHERE n >= ?
        """,
        [today - dt.timedelta(days=120), max(1, int(n_active * coverage_ratio))],
    ).fetchone()
    effective = row[0] if row and row[0] is not None else None
    start = (effective + dt.timedelta(days=1)) if effective else pd.to_datetime(get_config()["ingestion"]["history_start_date"]).date()
    if start > today:
        return effective, []
    return effective, get_trade_dates(conn, start.strftime("%Y%m%d"), today.strftime("%Y%m%d"))


def audit_qfq_basis(
    stored: pd.DataFrame,
    daily_raw: pd.DataFrame,
    adj_raw: pd.DataFrame,
    adj_latest: pd.DataFrame,
    rel_tol: float = 0.001,
) -> pd.DataFrame:
    """某一历史日 t：库内 qfq close 应等于 raw_close(t)·adj(t)/adj_latest。超出容差的票即“基准过期”
    （新浪逐股增量在除权后没有重定基历史行）。容差 = max(rel_tol, 半分钱/价格)。"""
    cols = ["symbol", "trade_date", "close_db", "close_expected", "rel"]
    if stored is None or stored.empty or daily_raw is None or daily_raw.empty:
        return pd.DataFrame(columns=cols)
    raw = normalize_bulk_daily(daily_raw)[["symbol", "trade_date", "close"]].rename(columns={"close": "raw_close"})
    adj_t = normalize_bulk_adj(adj_raw)[["symbol", "adj_factor"]].rename(columns={"adj_factor": "adj_t"})
    latest = adj_latest[["symbol", "adj_factor"]].rename(columns={"adj_factor": "adj_latest"})
    m = raw.merge(adj_t, on="symbol").merge(latest, on="symbol")
    m = m[(m["adj_latest"] > 0)]
    m["close_expected"] = m["raw_close"] * m["adj_t"] / m["adj_latest"]
    s = stored[["symbol", "close"]].rename(columns={"close": "close_db"})
    m = m.merge(s, on="symbol")
    m = m[m["close_db"] > 0]
    m["rel"] = m["close_expected"] / m["close_db"] - 1
    tol = pd.concat([pd.Series(rel_tol, index=m.index), _HALF_FEN / m["close_expected"]], axis=1).max(axis=1)
    return m.loc[m["rel"].abs() > tol, cols].reset_index(drop=True)


def pick_audit_dates(trade_dates: list[str], n_samples: int) -> list[str]:
    """从全历史交易日里挑审计采样日：最近 1/5/20/60 个交易日 + 全历史等距点，去重保序。"""
    if not trade_dates:
        return []
    idx: set[int] = set()
    n = len(trade_dates)
    for back in (1, 5, 20, 60):
        if n - back >= 0:
            idx.add(n - back)
    rest = max(0, n_samples - len(idx))
    if rest and n > 1:
        for k in range(rest):
            idx.add(int(round(k * (n - 1) / max(1, rest))))
    return [trade_dates[i] for i in sorted(idx) if 0 <= i < n]


def rebuild_symbol_qfq_history(conn, symbol: str, ts_code: str) -> int:
    """按票全历史重建 qfq（daily(ts_code) + adj_factor(ts_code)，复用 tushare_prices._raw_daily_to_qfq），
    保留库内已有的 turnover（新浪有、tushare daily 无）。2 次调用/票。"""
    from ingestion.tushare_prices import (
        _fetch_adj_factor,
        _fetch_listed_daily,
        _normalize_adj_factor,
        _raw_daily_to_qfq,
    )

    history_start = get_config()["ingestion"]["history_start_date"]
    limiter = tushare_limiter()
    limiter.wait()
    raw = retry_on_failure()(_fetch_listed_daily)(ts_code, history_start)
    limiter.wait()
    adj = _normalize_adj_factor(retry_on_failure()(_fetch_adj_factor)(ts_code), symbol)
    if raw is None or raw.empty or adj.empty:
        return 0
    norm = _raw_daily_to_qfq(raw, adj, symbol)
    if norm.empty:
        return 0
    old = conn.execute(
        "SELECT trade_date, turnover FROM daily_quotes WHERE symbol = ? AND adjust = 'qfq' AND turnover IS NOT NULL",
        [symbol],
    ).df()
    if not old.empty:
        old["trade_date"] = pd.to_datetime(old["trade_date"]).dt.date
        norm = norm.drop(columns=["turnover"]).merge(old, on="trade_date", how="left")
        norm = norm[BULK_QUOTES_COLS]
    conn.execute("BEGIN")
    try:
        upsert(conn, "adj_factor", ["symbol", "trade_date"], adj)
        conn.execute("DELETE FROM daily_quotes WHERE symbol = ? AND adjust = 'qfq'", [symbol])
        n = upsert(conn, "daily_quotes", ["symbol", "trade_date", "adjust"], norm)
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return n


def run_qfq_basis_audit(
    universe: dict[str, str],
    adj_latest: pd.DataFrame,
    effective_date: dt.date,
    *,
    n_samples: int,
    rel_tol: float,
) -> tuple[dict[str, list[str]], list[str]]:
    """抽样审计全库 qfq 基准：返回 ({symbol: [失配采样日...]}, 采样日列表)。每个采样日 2 次调用。"""
    history_start = get_config()["ingestion"]["history_start_date"]
    with write_session() as conn:
        dates = get_trade_dates(conn, history_start, effective_date.strftime("%Y%m%d"))
    sample = pick_audit_dates(dates, n_samples)
    limiter = tushare_limiter()
    fetch_daily = retry_on_failure()(_fetch_bulk_daily)
    fetch_adj = retry_on_failure()(_fetch_bulk_adj_factor)
    flagged: dict[str, list[str]] = {}
    for d in sample:
        limiter.wait()
        daily_raw = fetch_daily(d)
        limiter.wait()
        adj_raw = fetch_adj(d)
        d_date = pd.to_datetime(d, format="%Y%m%d").date()
        with write_session() as conn:
            stored = conn.execute(
                "SELECT symbol, close FROM daily_quotes WHERE adjust = 'qfq' AND trade_date = ?", [d_date]
            ).df()
        bad = audit_qfq_basis(stored, daily_raw, adj_raw, adj_latest, rel_tol)
        for s in bad["symbol"]:
            if s in universe:
                flagged.setdefault(str(s), []).append(d)
    logger.info("qfq 基准审计: 采样 %d 日，失配票 %d 只", len(sample), len(flagged))
    return flagged, sample


def sync_bulk_quotes_by_date(
    trade_dates: list[str] | None = None,
    limit: int | None = None,
    *,
    audit: bool = True,
    rebuild: bool = True,
) -> dict:
    """按交易日把 sh/sz/bj 全市场 qfq 日线补到今天；每日 3 次 Tushare 调用，顺序处理、每日一次短写。

    流程：取上一基准日 adj → 逐日 [daily, adj_factor, daily_basic] → 对 adj 跳变票重定基历史行
    → upsert 当日行与 adj_factor → （可选）抽样审计全库基准并按票重建失配票。
    """
    cfg = bulk_quotes_config()
    today = dt.date.today()
    with write_session(init=True) as conn:
        universe = dict(
            conn.execute(
                "SELECT symbol, exchange FROM universe WHERE exchange IN ('sh','sz','bj') AND is_delisted = FALSE"
            ).fetchall()
        )
        if trade_dates is None:
            effective, pending = bulk_pending_trade_dates(conn, cfg["coverage_ratio"], today)
        else:
            effective, pending = None, list(trade_dates)
            first = pd.to_datetime(pending[0], format="%Y%m%d").date() if pending else None
            if first is not None:
                row = conn.execute(
                    "SELECT max(trade_date) FROM daily_quotes WHERE adjust = 'qfq' AND trade_date < ?", [first]
                ).fetchone()
                effective = row[0] if row and row[0] is not None else None
    if limit:
        pending = pending[:limit]
    truncated = len(pending) > cfg["max_dates_per_run"]
    pending = pending[: cfg["max_dates_per_run"]]

    stats: dict[str, Any] = {
        "effective_date": str(effective) if effective else None,
        "dates_pending": len(pending),
        "dates_written": 0,
        "dates_empty": 0,
        "rows_written": 0,
        "adj_rows_written": 0,
        "readjusted_symbols": 0,
        "readjusted_rows": 0,
        "turnover_missing_dates": 0,
        "truncated": truncated,
        "audit": None,
        "rebuilt_symbols": 0,
        "rebuild_failed": [],
        "api_calls": 0,
    }
    if not pending:
        logger.info("sync_bulk_quotes_by_date: 无待补交易日（有效截面日 %s）", effective)
        return stats

    limiter = tushare_limiter()
    fetch_daily = retry_on_failure()(_fetch_bulk_daily)
    fetch_adj = retry_on_failure()(_fetch_bulk_adj_factor)
    fetch_turn = retry_on_failure()(_fetch_bulk_turnover)
    uni_set = set(universe)

    adj_prev: pd.DataFrame | None = None
    if effective is not None:
        limiter.wait()
        adj_prev = normalize_bulk_adj(fetch_adj(effective.strftime("%Y%m%d")))
        stats["api_calls"] += 1

    for d in pending:
        d_date = pd.to_datetime(d, format="%Y%m%d").date()
        limiter.wait()
        daily_raw = fetch_daily(d)
        stats["api_calls"] += 1
        if daily_raw is None or daily_raw.empty:
            # Tushare 尚未发布该日（收盘后约 16:00+ 才有）；更后面的日期也不会有，直接停
            stats["dates_empty"] += 1
            logger.info("sync_bulk_quotes_by_date: %s 无数据，停止", d)
            break
        limiter.wait()
        adj_today = normalize_bulk_adj(fetch_adj(d))
        stats["api_calls"] += 1
        turnover = None
        try:
            limiter.wait()
            turnover = fetch_turn(d)
            stats["api_calls"] += 1
        except Exception as exc:  # noqa: BLE001 - 换手率缺失不阻塞行情
            stats["turnover_missing_dates"] += 1
            logger.warning("sync_bulk_quotes_by_date: %s daily_basic 失败，turnover 留空: %s", d, exc)
        rows = normalize_bulk_daily(daily_raw, turnover, uni_set)
        ratios = compute_readjust_ratios(adj_prev, adj_today)
        with write_session() as conn:
            conn.execute("BEGIN")
            try:
                n_re = apply_readjust(conn, ratios, d_date)
                n = upsert(conn, "daily_quotes", ["symbol", "trade_date", "adjust"], rows)
                n_adj = upsert(conn, "adj_factor", ["symbol", "trade_date"], adj_today[adj_today["symbol"].isin(uni_set)])
                conn.execute(
                    "INSERT INTO sync_log (task_name, status, message) VALUES (?, ?, ?)",
                    ["sync_bulk_quotes_by_date", "success", f"date={d} rows={n} readjust_symbols={len(ratios)} readjust_rows={n_re}"],
                )
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise
        stats["dates_written"] += 1
        stats["rows_written"] += n
        stats["adj_rows_written"] += n_adj
        stats["readjusted_symbols"] += len(ratios)
        stats["readjusted_rows"] += n_re
        logger.info(
            "sync_bulk_quotes_by_date: %s rows=%d adj=%d readjust_symbols=%d readjust_rows=%d",
            d, n, n_adj, len(ratios), n_re,
        )
        adj_prev = adj_today

    if audit and adj_prev is not None and stats["dates_written"] > 0:
        last_date = pd.to_datetime(pending[stats["dates_written"] - 1], format="%Y%m%d").date()
        try:
            flagged, sample_dates = run_qfq_basis_audit(
                universe, adj_prev, last_date, n_samples=cfg["audit_sample_dates"], rel_tol=cfg["audit_rel_tol"]
            )
            stats["api_calls"] += 2 * len(sample_dates)
            stats["audit"] = {
                "sample_dates": len(sample_dates),
                "flagged_symbols": len(flagged),
                "sample": dict(list(flagged.items())[:10]),
            }
            if rebuild and flagged:
                todo = sorted(flagged)[: cfg["max_rebuild_symbols"]]
                logger.info("qfq 基准重建: %d 只（上限 %d）", len(todo), cfg["max_rebuild_symbols"])
                for i, s in enumerate(todo):
                    try:
                        with write_session() as conn:
                            rebuild_symbol_qfq_history(conn, s, to_ts_code(s, universe[s]))
                        stats["rebuilt_symbols"] += 1
                        stats["api_calls"] += 2
                    except Exception as exc:  # noqa: BLE001
                        stats["rebuild_failed"].append(s)
                        logger.warning("qfq 基准重建 %s 失败: %s: %s", s, type(exc).__name__, exc)
                    if (i + 1) % 50 == 0:
                        logger.info("qfq 基准重建进度 %d/%d", i + 1, len(todo))
        except Exception as exc:  # noqa: BLE001 - 审计失败不影响已写入的行情
            logger.warning("qfq 基准审计失败: %s: %s", type(exc).__name__, exc)
            stats["audit"] = {"error": f"{type(exc).__name__}: {exc}"}

    with write_session() as conn:
        conn.execute(
            "INSERT INTO sync_log (task_name, status, message) VALUES (?, ?, ?)",
            ["sync_bulk_quotes_by_date", "success", str(stats)[:500]],
        )
    logger.info("sync_bulk_quotes_by_date 完成: %s", stats)
    return stats


def sync_all_tushare_market_data(limit: int | None = None) -> dict:
    runners = {
        "daily_basic": sync_daily_basic,
        "index_weight": sync_index_weight,
        "suspend_calendar": sync_suspend_calendar,
        "limit_price": sync_limit_price,
    }
    results: dict[str, Any] = {}
    logger.info("=== Tushare 市值/指数成分/停复牌/涨跌停价格 开始 ===")
    for name in MARKET_SUBTASKS:
        if _pipeline_running() and _skip_market_subtask(name):
            logger.info("=== 跳过子任务 %s（本 run 步内断点已完成）===", name)
            results[name] = {"skipped_resume": True}
            continue
        results[name] = runners[name](limit=limit)
        if _pipeline_running():
            _record_market_subtask(name, results[name])
    logger.info("=== Tushare 市值/指数成分/停复牌/涨跌停价格 完成: %s ===", results)
    return results


if __name__ == "__main__":
    import argparse
    import os

    from common.db import sidecar_parallel_active

    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    if sidecar_parallel_active() and not os.environ.get("STOCK_QUANT_DB"):
        print("SKIP: Tushare 已在旁路库并行抓取，主库链跳过此步")
        raise SystemExit(0)

    parser = argparse.ArgumentParser(description="Tushare市值/指数成分/停复牌/涨跌停价格批量抓取")
    parser.add_argument(
        "--task",
        choices=["all", "daily_basic", "index_weight", "suspend", "limit_price", "bulk_quotes"],
        default="all",
    )
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--no-audit", action="store_true", help="bulk_quotes：跳过 qfq 基准抽样审计/重建")
    args = parser.parse_args()

    if args.task == "all":
        print(sync_all_tushare_market_data())
    elif args.task == "bulk_quotes":
        print(sync_bulk_quotes_by_date(limit=args.limit, audit=not args.no_audit, rebuild=not args.no_audit))
    elif args.task == "daily_basic":
        print(sync_daily_basic(limit=args.limit))
    elif args.task == "index_weight":
        print(sync_index_weight(limit=args.limit))
    elif args.task == "suspend":
        print(sync_suspend_calendar(limit=args.limit))
    elif args.task == "limit_price":
        print(sync_limit_price(limit=args.limit))
