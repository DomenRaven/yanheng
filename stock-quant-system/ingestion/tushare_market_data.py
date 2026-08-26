"""
Tushare Pro 集成 —— 每日市值指标 + 指数历史成分(point-in-time) + 停复牌/官方涨跌停价格。

解决 docs/phase0.6-secondary-gap-assessment.md 记录的：
    - 项目"总市值/流通市值批量"：daily_basic 直接提供交易所口径官方市值/PE/PB/PS，
      比自建 share_changes 拼接更权威（保留 corporate_actions.py 的股本变动数据作为交叉校验）
    - 项R"历史指数成分股变更"：此前免费源(csindex)只有当前快照，index_weight提供
      月度point-in-time历史成分和权重，首次真正解决"某股票在T日是否属于沪深300"
    - 项T"停牌/复牌历史"：此前 news_trade_notify_suspend_baidu 返回空表不可用，
      suspend_d 提供官方停复牌记录

额外拿到的官方涨跌停价格(limit_price)可与 research/a_share_rules.py 自算规则交叉校验。
"""
from __future__ import annotations

import datetime as dt
import logging

import pandas as pd

from common.config import get_config
from common.db import get_connection, init_schema
from common.tushare_client import (
    get_pro_api,
    get_trade_dates,
    get_universe_ts_codes,
    run_date_loop_sync,
    run_symbol_loop_sync,
)

logger = logging.getLogger("ingestion.tushare_market_data")

_CONSTITUENT_INDICES = {"000300.SH": "沪深300", "000905.SH": "中证500", "000852.SH": "中证1000"}


# ---------------------------------------------------------------------------
# 1. 每日市值/估值指标
# ---------------------------------------------------------------------------


def _fetch_daily_basic(ts_code: str) -> pd.DataFrame:
    cfg = get_config()
    start = cfg["ingestion"]["history_start_date"]
    return get_pro_api().daily_basic(ts_code=ts_code, start_date=start)


def _normalize_daily_basic(df: pd.DataFrame, symbol: str) -> pd.DataFrame:
    if df.empty:
        return df
    out = df.copy()
    out["symbol"] = symbol
    out["trade_date"] = pd.to_datetime(out["trade_date"], format="%Y%m%d", errors="coerce").dt.date
    out = out.dropna(subset=["trade_date"])
    cols = [
        "symbol", "trade_date", "close", "turnover_rate", "turnover_rate_f", "volume_ratio",
        "pe", "pe_ttm", "pb", "ps", "ps_ttm", "dv_ratio", "dv_ttm",
        "total_share", "float_share", "free_share", "total_mv", "circ_mv",
    ]
    for c in cols:
        if c not in out.columns:
            out[c] = None
    return out[cols].drop_duplicates(subset=["symbol", "trade_date"])


def sync_daily_basic(symbols: list[str] | None = None, limit: int | None = None) -> dict:
    conn = get_connection()
    init_schema(conn)
    targets = get_universe_ts_codes(conn, symbols, include_delisted=False)
    conn.close()
    return run_symbol_loop_sync(
        task_name="sync_daily_basic",
        table="daily_basic",
        key_cols=["symbol", "trade_date"],
        fetch_fn=_fetch_daily_basic,
        normalize_fn=_normalize_daily_basic,
        targets=targets,
        limit=limit,
    )


# ---------------------------------------------------------------------------
# 2. 指数历史成分（月度快照，point-in-time）
# ---------------------------------------------------------------------------


def _month_end_trade_dates(trade_dates: list[str]) -> list[str]:
    """从交易日列表里取每个自然月的最后一个交易日（不是自然月末，避开假期导致的空快照，
    2026-08-25 实测印证：2020-01-31是春节假期非交易日，若直接用自然月末查询会拿到空表）。"""
    df = pd.DataFrame({"d": pd.to_datetime(trade_dates, format="%Y%m%d")})
    df["ym"] = df["d"].dt.to_period("M")
    last_per_month = df.groupby("ym")["d"].max()
    return [d.strftime("%Y%m%d") for d in last_per_month]


def sync_index_weight(limit: int | None = None) -> dict:
    cfg = get_config()
    start = cfg["ingestion"]["history_start_date"]
    end = dt.date.today().strftime("%Y%m%d")

    conn = get_connection()
    init_schema(conn)
    trade_dates = get_trade_dates(conn, start, end)
    conn.close()
    month_ends = _month_end_trade_dates(trade_dates)
    if limit:
        month_ends = month_ends[:limit]

    stats = {"rows": 0, "failed": []}
    conn = get_connection()
    init_schema(conn)
    try:
        for index_code, name in _CONSTITUENT_INDICES.items():
            for trade_date in month_ends:
                try:
                    df = get_pro_api().index_weight(index_code=index_code, trade_date=trade_date)
                except Exception as exc:  # noqa: BLE001
                    stats["failed"].append((index_code, trade_date))
                    logger.warning("index_weight %s %s 失败: %s", index_code, trade_date, exc)
                    continue
                if df.empty:
                    continue
                out = df.rename(columns={"con_code": "symbol"}).copy()
                out["symbol"] = out["symbol"].str.split(".").str[0]
                out["trade_date"] = pd.to_datetime(out["trade_date"], format="%Y%m%d", errors="coerce").dt.date
                out = out.dropna(subset=["trade_date"])
                out = out[["index_code", "symbol", "trade_date", "weight"]]

                from common.tushare_client import upsert
                stats["rows"] += upsert(conn, "index_weight", ["index_code", "symbol", "trade_date"], out)

            logger.info("index_weight %s(%s) 完成，累计rows=%d", name, index_code, stats["rows"])

        conn.execute(
            "INSERT INTO sync_log (task_name, status, message) VALUES (?, ?, ?)",
            ["sync_index_weight", "success", str(stats)[:500]],
        )
        return stats
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# 3. 停复牌历史 + 官方涨跌停价格（逐交易日，市场快照）
# ---------------------------------------------------------------------------


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


def sync_all_tushare_market_data() -> dict:
    results = {}
    logger.info("=== Tushare 市值/指数成分/停复牌/涨跌停价格 开始 ===")
    results["daily_basic"] = sync_daily_basic()
    results["index_weight"] = sync_index_weight()
    results["suspend_calendar"] = sync_suspend_calendar()
    results["limit_price"] = sync_limit_price()
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
