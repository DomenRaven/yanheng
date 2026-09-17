"""
Tushare Pro 集成 —— Phase 2 行为金融层原始数据预取。

明确说明：这里只是把 docs/phase0.5-gap-closing-assessment.md 项J（"拥挤度/资金流/
龙虎榜"，此前因免费源未测试稳定性而维持排在Phase 2）里点名的数据源提前抓取落地，
不代表提前开工做行为金融建模——计划文档里"Phase 2才做心理学代理指标计算"的排期不变，
这批数据只是先备好，Phase 2 开工时不用再等抓取。

覆盖：
    - dragon_tiger_list（龙虎榜）+ block_trade（大宗交易）：用户明确提到的
      "大单买入/小单卖出"可疑盘口场景的原始数据
    - margin_balance（融资融券汇总）：杠杆资金/风险偏好代理指标
    - moneyflow（个股资金流向，大中小单分类）：羊群效应代理指标核心原料
    - pledge_stat（股权质押统计）：治理/流动性风险代理指标
    - holder_number（股东人数）：股权分散度/处置效应代理指标
"""
from __future__ import annotations

import datetime as dt
import logging

import pandas as pd
from tqdm import tqdm

from common.config import get_config
from common.db import get_connection, init_schema
from common.http_retry import polite_sleep, retry_on_failure
from common.tushare_client import (
    get_pro_api,
    get_trade_dates,
    get_universe_ts_codes,
    run_date_loop_sync,
    run_symbol_loop_sync,
    upsert,
)

logger = logging.getLogger("ingestion.tushare_behavior")


# ---------------------------------------------------------------------------
# 逐交易日：龙虎榜 / 大宗交易 / 融资融券汇总
# ---------------------------------------------------------------------------


def _fetch_top_list(trade_date: str) -> pd.DataFrame:
    return get_pro_api().top_list(trade_date=trade_date)


def _normalize_top_list(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty:
        return df
    out = df.rename(columns={"ts_code": "symbol", "pct_change": "pct_change"}).copy()
    out["symbol"] = out["symbol"].str.split(".").str[0]
    out["trade_date"] = pd.to_datetime(out["trade_date"], format="%Y%m%d", errors="coerce").dt.date
    out = out.dropna(subset=["trade_date"])
    cols = [
        "symbol", "trade_date", "name", "close", "pct_change", "turnover_rate", "amount",
        "l_sell", "l_buy", "l_amount", "net_amount", "net_rate", "amount_rate", "float_values", "reason",
    ]
    for c in cols:
        if c not in out.columns:
            out[c] = None
    out["reason"] = out["reason"].fillna("").astype(str)
    return out[cols].drop_duplicates(subset=["symbol", "trade_date", "reason"])


def sync_dragon_tiger_list(limit: int | None = None) -> dict:
    cfg = get_config()
    start, end = cfg["ingestion"]["history_start_date"], dt.date.today().strftime("%Y%m%d")
    conn = get_connection()
    init_schema(conn)
    trade_dates = get_trade_dates(conn, start, end)
    conn.close()
    return run_date_loop_sync(
        "sync_dragon_tiger_list", "dragon_tiger_list", ["symbol", "trade_date", "reason"],
        _fetch_top_list, _normalize_top_list, trade_dates, limit=limit,
    )


def _fetch_block_trade(trade_date: str) -> pd.DataFrame:
    return get_pro_api().block_trade(trade_date=trade_date)


def _normalize_block_trade(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty:
        return df
    out = df.rename(columns={"ts_code": "symbol"}).copy()
    out["symbol"] = out["symbol"].str.split(".").str[0]
    out["trade_date"] = pd.to_datetime(out["trade_date"], format="%Y%m%d", errors="coerce").dt.date
    out = out.dropna(subset=["trade_date"])
    out["buyer"] = out["buyer"].fillna("").astype(str)
    out["seller"] = out["seller"].fillna("").astype(str)
    return out[["symbol", "trade_date", "price", "vol", "amount", "buyer", "seller"]].drop_duplicates(
        subset=["symbol", "trade_date", "price", "vol", "buyer", "seller"]
    )


def sync_block_trade(limit: int | None = None) -> dict:
    cfg = get_config()
    start, end = cfg["ingestion"]["history_start_date"], dt.date.today().strftime("%Y%m%d")
    conn = get_connection()
    init_schema(conn)
    trade_dates = get_trade_dates(conn, start, end)
    conn.close()
    return run_date_loop_sync(
        "sync_block_trade", "block_trade", ["symbol", "trade_date", "price", "vol", "buyer", "seller"],
        _fetch_block_trade, _normalize_block_trade, trade_dates, limit=limit,
    )


def _fetch_margin(trade_date: str) -> pd.DataFrame:
    return get_pro_api().margin(trade_date=trade_date)


def _normalize_margin(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty:
        return df
    out = df.copy()
    out["trade_date"] = pd.to_datetime(out["trade_date"], format="%Y%m%d", errors="coerce").dt.date
    out = out.dropna(subset=["trade_date"])
    return out[["exchange_id", "trade_date", "rzye", "rzmre", "rzche", "rqye", "rqmcl", "rzrqye", "rqyl"]]


def sync_margin_balance(limit: int | None = None) -> dict:
    cfg = get_config()
    start, end = cfg["ingestion"]["history_start_date"], dt.date.today().strftime("%Y%m%d")
    conn = get_connection()
    init_schema(conn)
    trade_dates = get_trade_dates(conn, start, end)
    conn.close()
    return run_date_loop_sync(
        "sync_margin_balance", "margin_balance", ["exchange_id", "trade_date"],
        _fetch_margin, _normalize_margin, trade_dates, limit=limit,
    )


# ---------------------------------------------------------------------------
# 逐股票：个股资金流向 / 股权质押统计 / 股东人数
# ---------------------------------------------------------------------------


def _fetch_moneyflow(ts_code: str) -> pd.DataFrame:
    cfg = get_config()
    start = cfg["ingestion"]["history_start_date"]
    return get_pro_api().moneyflow(ts_code=ts_code, start_date=start)


def _normalize_moneyflow(df: pd.DataFrame, symbol: str) -> pd.DataFrame:
    if df.empty:
        return df
    out = df.copy()
    out["symbol"] = symbol
    out["trade_date"] = pd.to_datetime(out["trade_date"], format="%Y%m%d", errors="coerce").dt.date
    out = out.dropna(subset=["trade_date"])
    cols = [
        "symbol", "trade_date", "buy_sm_vol", "buy_sm_amount", "sell_sm_vol", "sell_sm_amount",
        "buy_md_vol", "buy_md_amount", "sell_md_vol", "sell_md_amount", "buy_lg_vol", "buy_lg_amount",
        "sell_lg_vol", "sell_lg_amount", "buy_elg_vol", "buy_elg_amount", "sell_elg_vol", "sell_elg_amount",
        "net_mf_vol", "net_mf_amount",
    ]
    for c in cols:
        if c not in out.columns:
            out[c] = None
    return out[cols].drop_duplicates(subset=["symbol", "trade_date"])


def sync_moneyflow(symbols: list[str] | None = None, limit: int | None = None) -> dict:
    conn = get_connection()
    init_schema(conn)
    targets = get_universe_ts_codes(conn, symbols, include_delisted=False)
    conn.close()
    return run_symbol_loop_sync(
        "sync_moneyflow", "moneyflow", ["symbol", "trade_date"],
        _fetch_moneyflow, _normalize_moneyflow, targets, limit=limit,
    )


def _fetch_pledge_stat(ts_code: str) -> pd.DataFrame:
    return get_pro_api().pledge_stat(ts_code=ts_code)


def _normalize_pledge_stat(df: pd.DataFrame, symbol: str) -> pd.DataFrame:
    if df.empty:
        return df
    out = df.copy()
    out["symbol"] = symbol
    out["end_date"] = pd.to_datetime(out["end_date"], format="%Y%m%d", errors="coerce").dt.date
    out = out.dropna(subset=["end_date"])
    cols = ["symbol", "end_date", "pledge_count", "unrest_pledge", "rest_pledge", "total_share", "pledge_ratio"]
    for c in cols:
        if c not in out.columns:
            out[c] = None
    return out[cols].drop_duplicates(subset=["symbol", "end_date"])


def sync_pledge_stat(symbols: list[str] | None = None, limit: int | None = None) -> dict:
    conn = get_connection()
    init_schema(conn)
    targets = get_universe_ts_codes(conn, symbols, include_delisted=False)
    conn.close()
    return run_symbol_loop_sync(
        "sync_pledge_stat", "pledge_stat", ["symbol", "end_date"],
        _fetch_pledge_stat, _normalize_pledge_stat, targets, limit=limit,
    )


def _fetch_holder_number(ts_code: str) -> pd.DataFrame:
    return get_pro_api().stk_holdernumber(ts_code=ts_code)


def _normalize_holder_number(df: pd.DataFrame, symbol: str) -> pd.DataFrame:
    if df.empty:
        return df
    out = df.copy()
    out["symbol"] = symbol
    out["ann_date"] = pd.to_datetime(out["ann_date"], format="%Y%m%d", errors="coerce").dt.date
    out["end_date"] = pd.to_datetime(out["end_date"], format="%Y%m%d", errors="coerce").dt.date
    out = out.dropna(subset=["ann_date"])
    return out[["symbol", "ann_date", "end_date", "holder_num"]].drop_duplicates(subset=["symbol", "ann_date"])


def sync_holder_number(symbols: list[str] | None = None, limit: int | None = None) -> dict:
    conn = get_connection()
    init_schema(conn)
    targets = get_universe_ts_codes(conn, symbols, include_delisted=False)
    conn.close()
    return run_symbol_loop_sync(
        "sync_holder_number", "holder_number", ["symbol", "ann_date"],
        _fetch_holder_number, _normalize_holder_number, targets, limit=limit,
    )


# 沪港通 2014-11-17 开通；接口每次最多约 300 行，按日历窗口切块
_HSGT_START = "20141117"
_HSGT_CHUNK_DAYS = 180


def _fetch_moneyflow_hsgt(start_date: str, end_date: str) -> pd.DataFrame:
    return get_pro_api().moneyflow_hsgt(start_date=start_date, end_date=end_date)


def _normalize_moneyflow_hsgt(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty:
        return df
    out = df.copy()
    out["trade_date"] = pd.to_datetime(out["trade_date"], format="%Y%m%d", errors="coerce").dt.date
    out = out.dropna(subset=["trade_date"])
    cols = ["trade_date", "ggt_ss", "ggt_sz", "hgt", "sgt", "north_money", "south_money"]
    for c in cols:
        if c not in out.columns:
            out[c] = None
    return out[cols].drop_duplicates(subset=["trade_date"])


def sync_moneyflow_hsgt(limit: int | None = None) -> dict:
    """全市场按日一条的北向/南向资金。金额单位沿用 Tushare：百万元。"""
    cfg_start = get_config()["ingestion"]["history_start_date"]
    conn = get_connection()
    init_schema(conn)
    last = conn.execute("SELECT max(trade_date) FROM moneyflow_hsgt").fetchone()[0]
    start = max(_HSGT_START, cfg_start)
    if last is not None:
        start = (pd.Timestamp(last) + pd.Timedelta(days=1)).strftime("%Y%m%d")
    end = dt.date.today().strftime("%Y%m%d")

    windows: list[tuple[str, str]] = []
    cur = pd.Timestamp(start)
    end_ts = pd.Timestamp(end)
    while cur <= end_ts:
        wend = min(cur + pd.Timedelta(days=_HSGT_CHUNK_DAYS - 1), end_ts)
        windows.append((cur.strftime("%Y%m%d"), wend.strftime("%Y%m%d")))
        cur = wend + pd.Timedelta(days=1)
    if limit:
        windows = windows[:limit]

    stats = {"ok": 0, "empty": 0, "failed": 0, "rows_written": 0, "failed_windows": [], "skipped": 0}
    if not windows:
        stats["skipped"] = 1
        conn.execute(
            "INSERT INTO sync_log (task_name, status, message) VALUES (?, ?, ?)",
            ["sync_moneyflow_hsgt", "success", str(stats)[:500]],
        )
        conn.close()
        logger.info("sync_moneyflow_hsgt 已是最新: %s", stats)
        return stats

    fetch = retry_on_failure()(_fetch_moneyflow_hsgt)
    for start_d, end_d in tqdm(windows, desc="sync_moneyflow_hsgt"):
        try:
            raw = fetch(start_d, end_d)
            if len(raw) >= 300:
                logger.warning("moneyflow_hsgt %s-%s 返回 %d 行，可能触顶截断", start_d, end_d, len(raw))
            norm = _normalize_moneyflow_hsgt(raw)
            if norm.empty:
                stats["empty"] += 1
            else:
                stats["rows_written"] += upsert(conn, "moneyflow_hsgt", ["trade_date"], norm)
                stats["ok"] += 1
        except Exception as exc:  # noqa: BLE001
            stats["failed"] += 1
            stats["failed_windows"].append(f"{start_d}-{end_d}")
            logger.warning("sync_moneyflow_hsgt: %s-%s 失败: %s: %s", start_d, end_d, type(exc).__name__, exc)
        polite_sleep()

    conn.execute(
        "INSERT INTO sync_log (task_name, status, message) VALUES (?, ?, ?)",
        ["sync_moneyflow_hsgt", "success", str(stats)[:500]],
    )
    conn.close()
    logger.info("sync_moneyflow_hsgt 完成: %s", stats)
    return stats


def sync_all_tushare_behavior(limit: int | None = None) -> dict:
    results = {}
    logger.info("=== Tushare Phase2行为金融原始数据预取 开始 ===")
    results["dragon_tiger_list"] = sync_dragon_tiger_list(limit=limit)
    results["block_trade"] = sync_block_trade(limit=limit)
    results["margin_balance"] = sync_margin_balance(limit=limit)
    results["moneyflow"] = sync_moneyflow(limit=limit)
    results["moneyflow_hsgt"] = sync_moneyflow_hsgt(limit=limit)
    results["pledge_stat"] = sync_pledge_stat(limit=limit)
    results["holder_number"] = sync_holder_number(limit=limit)
    logger.info("=== Tushare Phase2行为金融原始数据预取 完成: %s ===", results)
    return results


if __name__ == "__main__":
    import argparse
    import os

    from common.db import sidecar_parallel_active

    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    if sidecar_parallel_active() and not os.environ.get("STOCK_QUANT_DB"):
        print("SKIP: Tushare 已在旁路库并行抓取，主库链跳过此步")
        raise SystemExit(0)

    parser = argparse.ArgumentParser(description="Tushare Phase2行为金融原始数据预取")
    parser.add_argument(
        "--task",
        choices=["all", "dragon_tiger", "block_trade", "margin", "moneyflow", "hsgt", "pledge", "holder_number"],
        default="all",
    )
    parser.add_argument("--limit", type=int, default=None)
    args = parser.parse_args()

    if args.task == "all":
        print(sync_all_tushare_behavior())
    elif args.task == "dragon_tiger":
        print(sync_dragon_tiger_list(limit=args.limit))
    elif args.task == "block_trade":
        print(sync_block_trade(limit=args.limit))
    elif args.task == "margin":
        print(sync_margin_balance(limit=args.limit))
    elif args.task == "moneyflow":
        print(sync_moneyflow(limit=args.limit))
    elif args.task == "hsgt":
        print(sync_moneyflow_hsgt(limit=args.limit))
    elif args.task == "pledge":
        print(sync_pledge_stat(limit=args.limit))
    elif args.task == "holder_number":
        print(sync_holder_number(limit=args.limit))
