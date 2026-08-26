"""
Phase 0.6 数据缺口补齐（第二轮）—— 轻量批次：交易日历 + 基准指数日线 + 指数成分快照。

三个子任务均为"全市场少量请求覆盖"设计（合计约8次请求），预期耗时分钟级，
区别于 corporate_actions.py / income_statement_batch.py 的逐股票小时级批处理。

必要性与可实现性评估过程见 docs/phase0.6-secondary-gap-assessment.md。
"""
from __future__ import annotations

import datetime as dt
import logging

import akshare as ak
import pandas as pd

from common.db import get_connection, init_schema
from common.http_retry import FetchFailedError, polite_sleep, retry_on_failure

logger = logging.getLogger("ingestion.market_data")

# 东财源(index_zh_a_hist)在本机代理环境下实测 ProxyError，改用新浪源(stock_zh_index_daily)。
_BENCHMARK_INDICES = {
    "sh000300": "沪深300",
    "sh000905": "中证500",
    "sh000852": "中证1000",
    "sh000001": "上证指数",
}

# 中证指数官方源(csindex)，与东财无关，实测稳定。symbol 用不带交易所前缀的6位指数代码。
_CONSTITUENT_INDICES = {
    "000300": "沪深300",
    "000905": "中证500",
    "000852": "中证1000",
}


# ---------------------------------------------------------------------------
# 1. 交易日历
# ---------------------------------------------------------------------------


@retry_on_failure()
def _fetch_trade_calendar() -> pd.DataFrame:
    return ak.tool_trade_date_hist_sina()


def sync_trade_calendar() -> dict:
    raw = _fetch_trade_calendar()
    raw = raw.rename(columns={raw.columns[0]: "trade_date"})
    raw["trade_date"] = pd.to_datetime(raw["trade_date"], errors="coerce").dt.date
    raw = raw.dropna(subset=["trade_date"]).drop_duplicates()

    conn = get_connection()
    init_schema(conn)
    try:
        conn.register("incoming_calendar", raw[["trade_date"]])
        conn.execute(
            """
            INSERT INTO trade_calendar (trade_date)
            SELECT trade_date FROM incoming_calendar
            WHERE trade_date NOT IN (SELECT trade_date FROM trade_calendar)
            """
        )
        conn.unregister("incoming_calendar")
        n_rows = conn.execute("SELECT count(*) FROM trade_calendar").fetchone()[0]
        conn.execute(
            "INSERT INTO sync_log (task_name, status, message) VALUES (?, ?, ?)",
            ["sync_trade_calendar", "success", f"total_rows={n_rows}"],
        )
        logger.info("交易日历同步完成: 共%d个交易日", n_rows)
        return {"rows": n_rows}
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# 2. 基准指数日线
# ---------------------------------------------------------------------------


@retry_on_failure()
def _fetch_index_daily(index_code: str) -> pd.DataFrame:
    return ak.stock_zh_index_daily(symbol=index_code)


def sync_index_quotes() -> dict:
    conn = get_connection()
    init_schema(conn)
    total_rows = 0
    failed: list[str] = []
    try:
        for index_code, name in _BENCHMARK_INDICES.items():
            try:
                df = _fetch_index_daily(index_code)
            except FetchFailedError as exc:
                failed.append(index_code)
                logger.warning("指数日线 %s(%s) 抓取失败: %s", name, index_code, exc)
                conn.execute(
                    "INSERT INTO sync_log (task_name, symbol, status, message) VALUES (?, ?, ?, ?)",
                    ["sync_index_quotes", index_code, "failed", str(exc)[:200]],
                )
                polite_sleep()
                continue

            df["date"] = pd.to_datetime(df["date"], errors="coerce").dt.date
            df = df.dropna(subset=["date"])
            df["index_code"] = index_code
            df = df.rename(columns={"date": "trade_date"})
            df = df[["index_code", "trade_date", "open", "high", "low", "close", "volume"]]

            conn.register("incoming_index_quotes", df)
            conn.execute(
                """
                DELETE FROM index_quotes
                WHERE (index_code, trade_date) IN (
                    SELECT index_code, trade_date FROM incoming_index_quotes
                )
                """
            )
            conn.execute(
                """
                INSERT INTO index_quotes (index_code, trade_date, open, high, low, close, volume)
                SELECT index_code, trade_date, open, high, low, close, volume FROM incoming_index_quotes
                """
            )
            conn.unregister("incoming_index_quotes")

            total_rows += len(df)
            logger.info("指数日线 %s(%s) 完成: %d条", name, index_code, len(df))
            conn.execute(
                "INSERT INTO sync_log (task_name, symbol, status, message) VALUES (?, ?, ?, ?)",
                ["sync_index_quotes", index_code, "success", f"rows={len(df)}"],
            )
            polite_sleep()

        return {"rows": total_rows, "failed": failed}
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# 3. 指数成分股（当前快照，无免费历史成分变更源，靠周期性重跑累积历史）
# ---------------------------------------------------------------------------


@retry_on_failure()
def _fetch_index_constituents(index_code: str) -> pd.DataFrame:
    return ak.index_stock_cons_csindex(symbol=index_code)


def sync_index_constituents() -> dict:
    today = dt.date.today()
    conn = get_connection()
    init_schema(conn)
    total_rows = 0
    failed: list[str] = []
    try:
        for index_code, name in _CONSTITUENT_INDICES.items():
            try:
                df = _fetch_index_constituents(index_code)
            except FetchFailedError as exc:
                failed.append(index_code)
                logger.warning("指数成分 %s(%s) 抓取失败: %s", name, index_code, exc)
                conn.execute(
                    "INSERT INTO sync_log (task_name, symbol, status, message) VALUES (?, ?, ?, ?)",
                    ["sync_index_constituents", index_code, "failed", str(exc)[:200]],
                )
                polite_sleep()
                continue

            df = df.rename(columns={"成分券代码": "symbol"})
            df["symbol"] = df["symbol"].astype(str).str.zfill(6)
            out = pd.DataFrame(
                {
                    "index_code": index_code,
                    "snapshot_date": today,
                    "symbol": df["symbol"],
                }
            ).drop_duplicates()

            conn.register("incoming_constituents", out)
            conn.execute(
                """
                DELETE FROM index_constituents
                WHERE (index_code, snapshot_date) IN (
                    SELECT DISTINCT index_code, snapshot_date FROM incoming_constituents
                )
                """
            )
            conn.execute(
                "INSERT INTO index_constituents (index_code, snapshot_date, symbol) SELECT * FROM incoming_constituents"
            )
            conn.unregister("incoming_constituents")

            total_rows += len(out)
            logger.info("指数成分 %s(%s) 快照完成: %d只", name, index_code, len(out))
            conn.execute(
                "INSERT INTO sync_log (task_name, symbol, status, message) VALUES (?, ?, ?, ?)",
                ["sync_index_constituents", index_code, "success", f"rows={len(out)}"],
            )
            polite_sleep()

        return {"rows": total_rows, "failed": failed}
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# 编排入口
# ---------------------------------------------------------------------------


def sync_all_market_data() -> dict:
    results = {}
    logger.info("=== Phase 0.6 轻量批次开始 ===")
    results["trade_calendar"] = sync_trade_calendar()
    results["index_quotes"] = sync_index_quotes()
    results["index_constituents"] = sync_index_constituents()
    logger.info("=== Phase 0.6 轻量批次完成: %s ===", results)
    return results


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    print(sync_all_market_data())
