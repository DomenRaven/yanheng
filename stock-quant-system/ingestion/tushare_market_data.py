"""
Tushare Pro 集成 —— 每日市值指标 + 指数历史成分(point-in-time) + 停复牌/官方涨跌停价格。

daily_basic 默认按 trade_date 拉全市场快照（~2600 次/全历史），替代逐股 ~5500 次；
步内子任务断点见 common.ingestion_engine.pipeline_state.substeps。
"""
from __future__ import annotations

import datetime as dt
import logging
from typing import Any

import pandas as pd

from common.config import get_config
from common.db import get_connection, init_schema
from common.ingestion_engine.pipeline_state import load_state, mark_substep, substep_is_ok
from common.tushare_client import (
    get_pro_api,
    get_trade_dates,
    run_date_loop_sync,
    run_item_loop_sync,
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
        "--task", choices=["all", "daily_basic", "index_weight", "suspend", "limit_price"], default="all"
    )
    parser.add_argument("--limit", type=int, default=None)
    args = parser.parse_args()

    if args.task == "all":
        print(sync_all_tushare_market_data())
    elif args.task == "daily_basic":
        print(sync_daily_basic(limit=args.limit))
    elif args.task == "index_weight":
        print(sync_index_weight(limit=args.limit))
    elif args.task == "suspend":
        print(sync_suspend_calendar(limit=args.limit))
    elif args.task == "limit_price":
        print(sync_limit_price(limit=args.limit))
