"""
全市场日线行情批量/增量抓取。

数据源策略（对应 config.yaml -> ingestion.primary_source/fallback_source）：
    - 沪深(sh/sz)股票：优先用新浪财经接口 ak.stock_zh_a_daily，实测在当前网络环境下最稳定。
    - 北交所(bj)股票：新浪接口不支持，回退到东方财富接口 ak.stock_zh_a_hist。
      该接口在存在本地代理/VPN的机器上可能出现间歇性 502/连接重置，
      因此单独统计失败数，不阻塞沪深股票的抓取进度。

增量更新逻辑：
    对每只股票，查询 daily_quotes 中已有的最大 trade_date；
    若存在，则从"最大日期+1天"开始补抓到今天；若不存在（新股票/首次抓取），
    从 config.yaml -> ingestion.history_start_date 开始抓取全量历史。
    这样脚本可随时中断、重新运行，天然支持断点续传，不会重复抓取已有数据。
"""
from __future__ import annotations

import datetime as dt
import logging
import time

import akshare as ak
import pandas as pd
from tqdm import tqdm

from common.config import get_config
from common.db import get_connection, init_schema
from common.http_retry import FetchFailedError, polite_sleep, retry_on_failure

logger = logging.getLogger("ingestion.quotes_batch")

_SINA_COLUMN_MAP = {
    "date": "trade_date",
    "open": "open",
    "high": "high",
    "low": "low",
    "close": "close",
    "volume": "volume",
    "amount": "amount",
    "turnover": "turnover",
}

_EM_COLUMN_MAP = {
    "日期": "trade_date",
    "开盘": "open",
    "收盘": "close",
    "最高": "high",
    "最低": "low",
    "成交量": "volume",
    "成交额": "amount",
    "涨跌幅": "pct_change",
    "换手率": "turnover",
}


@retry_on_failure()
def _fetch_sina_daily(symbol: str, exchange: str, start_date: str, end_date: str, adjust: str) -> pd.DataFrame:
    sina_symbol = f"{exchange}{symbol}"
    return ak.stock_zh_a_daily(symbol=sina_symbol, start_date=start_date, end_date=end_date, adjust=adjust)


@retry_on_failure()
def _fetch_em_hist(symbol: str, start_date: str, end_date: str, adjust: str) -> pd.DataFrame:
    return ak.stock_zh_a_hist(symbol=symbol, period="daily", start_date=start_date, end_date=end_date, adjust=adjust)


def _normalize_sina(df: pd.DataFrame, symbol: str, adjust: str) -> pd.DataFrame:
    df = df.rename(columns=_SINA_COLUMN_MAP)
    df["symbol"] = symbol
    df["adjust"] = adjust
    if "pct_change" not in df.columns:
        df["pct_change"] = None
    keep = ["symbol", "trade_date", "open", "high", "low", "close", "volume", "amount", "turnover", "pct_change", "adjust"]
    return df[keep]


def _normalize_em(df: pd.DataFrame, symbol: str, adjust: str) -> pd.DataFrame:
    df = df.rename(columns=_EM_COLUMN_MAP)
    df["symbol"] = symbol
    df["adjust"] = adjust
    keep = ["symbol", "trade_date", "open", "high", "low", "close", "volume", "amount", "turnover", "pct_change", "adjust"]
    for col in keep:
        if col not in df.columns:
            df[col] = None
    return df[keep]


def _get_sync_targets(conn, symbols: list[str] | None) -> list[tuple[str, str, str | None]]:
    """返回 [(symbol, exchange, last_trade_date_or_None), ...]"""
    where = "WHERE is_delisted = FALSE"
    params: list = []
    if symbols:
        placeholders = ",".join(["?"] * len(symbols))
        where += f" AND symbol IN ({placeholders})"
        params = list(symbols)

    universe_rows = conn.execute(f"SELECT symbol, exchange FROM universe {where}", params).fetchall()
    last_dates = dict(
        conn.execute(
            "SELECT symbol, max(trade_date) FROM daily_quotes GROUP BY symbol"
        ).fetchall()
    )
    return [(sym, exch, last_dates.get(sym)) for sym, exch in universe_rows]


def _upsert_quotes(conn, df: pd.DataFrame) -> int:
    if df.empty:
        return 0
    df = df.dropna(subset=["trade_date"])
    if df.empty:
        return 0
    conn.register("incoming_quotes", df)
    conn.execute(
        """
        DELETE FROM daily_quotes
        WHERE (symbol, trade_date, adjust) IN (
            SELECT symbol, trade_date, adjust FROM incoming_quotes
        )
        """
    )
    conn.execute(
        """
        INSERT INTO daily_quotes
            (symbol, trade_date, open, high, low, close, volume, amount, turnover, pct_change, adjust)
        SELECT symbol, trade_date, open, high, low, close, volume, amount, turnover, pct_change, adjust
        FROM incoming_quotes
        """
    )
    conn.unregister("incoming_quotes")
    return len(df)


def sync_quotes_batch(symbols: list[str] | None = None, limit: int | None = None) -> dict:
    cfg = get_config()["ingestion"]
    history_start = cfg["history_start_date"]
    adjust = cfg["adjust"]
    checkpoint_size = cfg.get("checkpoint_batch_size", 50)
    today_str = dt.date.today().strftime("%Y%m%d")

    conn = get_connection()
    init_schema(conn)

    targets = _get_sync_targets(conn, symbols)
    if limit:
        targets = targets[:limit]

    stats = {"ok": 0, "skipped_up_to_date": 0, "failed": 0, "rows_written": 0, "failed_symbols": []}

    for i, (symbol, exchange, last_date) in enumerate(tqdm(targets, desc="quotes_batch")):
        start_date = (last_date + dt.timedelta(days=1)).strftime("%Y%m%d") if last_date else history_start
        if start_date > today_str:
            stats["skipped_up_to_date"] += 1
            continue

        try:
            if exchange in ("sh", "sz"):
                raw = _fetch_sina_daily(symbol, exchange, start_date, today_str, adjust)
                norm = _normalize_sina(raw, symbol, adjust)
            elif exchange == "bj":
                raw = _fetch_em_hist(symbol, start_date, today_str, adjust)
                norm = _normalize_em(raw, symbol, adjust)
            else:
                stats["failed"] += 1
                stats["failed_symbols"].append(symbol)
                continue

            n = _upsert_quotes(conn, norm)
            stats["rows_written"] += n
            stats["ok"] += 1
            conn.execute(
                "INSERT INTO sync_log (task_name, symbol, status, message) VALUES (?, ?, ?, ?)",
                ["sync_quotes_batch", symbol, "success", f"rows={n}, range={start_date}-{today_str}"],
            )
        except FetchFailedError as exc:
            stats["failed"] += 1
            stats["failed_symbols"].append(symbol)
            conn.execute(
                "INSERT INTO sync_log (task_name, symbol, status, message) VALUES (?, ?, ?, ?)",
                ["sync_quotes_batch", symbol, "failed", str(exc)[:300]],
            )
            logger.warning("%s 抓取失败，已记录，跳过: %s", symbol, exc)

        if (i + 1) % checkpoint_size == 0:
            conn.commit() if hasattr(conn, "commit") else None
            logger.info(
                "进度 %d/%d: ok=%d failed=%d rows=%d",
                i + 1, len(targets), stats["ok"], stats["failed"], stats["rows_written"],
            )

        polite_sleep()

    conn.close()
    logger.info("批量行情同步完成: %s", stats)
    return stats


if __name__ == "__main__":
    import argparse

    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    parser = argparse.ArgumentParser(description="全市场日线行情批量/增量抓取")
    parser.add_argument("--limit", type=int, default=None, help="仅同步前N只股票（调试用）")
    parser.add_argument("--symbols", type=str, default=None, help="逗号分隔的股票代码列表，仅同步指定股票")
    args = parser.parse_args()

    symbols = args.symbols.split(",") if args.symbols else None
    result = sync_quotes_batch(symbols=symbols, limit=args.limit)
    print(result)
