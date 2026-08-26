"""
Tushare Pro 集成 —— 复权因子 + 退市股历史行情。

解决两个此前免费源（akshare/sina/eastmoney）验证失败或成本过高的已知缺口：
    - 复权因子(adj_factor)：docs/phase0.6-secondary-gap-assessment.md 项S，
      此前评估"技术可行但成本=重跑一次全市场行情批量"，Tushare一次调用每只股票
      返回全部历史复权因子，成本降为与fundamentals_batch同量级的逐股票循环。
    - 退市股历史行情：docs/phase0.5-gap-closing-assessment.md 项B，此前 sina/eastmoney
      均无法取得（JSONDecodeError/ProxyError），Tushare的daily接口实测对退市股同样
      有效（含最早2002年数据），直接缓解全项目最大的已知幸存者偏差缺口。

已知限制：退市股行情存的是未复权价格（adjust='raw'），不是qfq；若后续需要复权序列，
可用同批抓取的 adj_factor 换算（qfq = raw * adj_factor(当日) / adj_factor(基准日)）。
"""
from __future__ import annotations

import logging

import pandas as pd
from tqdm import tqdm

from common.config import get_config
from common.db import get_connection, init_schema
from common.http_retry import polite_sleep, retry_on_failure
from common.tushare_client import (
    get_pro_api,
    get_universe_ts_codes,
    run_symbol_loop_sync,
    to_ts_code,
    upsert,
)

logger = logging.getLogger("ingestion.tushare_prices")


def _fetch_adj_factor(ts_code: str) -> pd.DataFrame:
    return get_pro_api().adj_factor(ts_code=ts_code)


def _normalize_adj_factor(df: pd.DataFrame, symbol: str) -> pd.DataFrame:
    if df.empty:
        return df
    out = df.rename(columns={"trade_date": "trade_date"}).copy()
    out["symbol"] = symbol
    out["trade_date"] = pd.to_datetime(out["trade_date"], format="%Y%m%d", errors="coerce").dt.date
    out = out.dropna(subset=["trade_date"])
    return out[["symbol", "trade_date", "adj_factor"]].drop_duplicates(subset=["symbol", "trade_date"])


def sync_adj_factor(symbols: list[str] | None = None, limit: int | None = None, include_delisted: bool = True) -> dict:
    conn = get_connection()
    init_schema(conn)
    targets = get_universe_ts_codes(conn, symbols, include_delisted=include_delisted)
    conn.close()
    return run_symbol_loop_sync(
        task_name="sync_adj_factor",
        table="adj_factor",
        key_cols=["symbol", "trade_date"],
        fetch_fn=_fetch_adj_factor,
        normalize_fn=_normalize_adj_factor,
        targets=targets,
        limit=limit,
    )


def _fetch_delisted_daily(ts_code: str) -> pd.DataFrame:
    cfg_start = "20000101"  # 退市股可能在系统history_start_date(2015)之前就已退市，尽量拉全历史
    return get_pro_api().daily(ts_code=ts_code, start_date=cfg_start)


def _normalize_delisted_daily(df: pd.DataFrame, symbol: str) -> pd.DataFrame:
    if df.empty:
        return df
    out = df.rename(
        columns={"vol": "volume", "amount": "amount", "pct_chg": "pct_change"}
    ).copy()
    out["symbol"] = symbol
    out["trade_date"] = pd.to_datetime(out["trade_date"], format="%Y%m%d", errors="coerce").dt.date
    out = out.dropna(subset=["trade_date"])
    # 2026-08-26 修复：Tushare daily 接口 vol 单位是"手"(100股)、amount 单位是"千元"，
    # 沪深库(sina源)的 daily_quotes.volume/amount 单位分别是"股"/"元"——统一换算，
    # 否则跨源volume/amount口径不一致（真实bug，behavior/chip_distribution.py验证时发现，
    # 已用SQL直接修正当时已写入的1,073,337行历史数据，见 docs/phase2-acceptance-report.md）。
    out["volume"] = pd.to_numeric(out["volume"], errors="coerce") * 100.0
    out["amount"] = pd.to_numeric(out["amount"], errors="coerce") * 1000.0
    out["turnover"] = None
    out["adjust"] = "raw"
    return out[
        ["symbol", "trade_date", "open", "high", "low", "close", "volume", "amount", "turnover", "pct_change", "adjust"]
    ].drop_duplicates(subset=["symbol", "trade_date", "adjust"])


def sync_delisted_daily_quotes(symbols: list[str] | None = None, limit: int | None = None) -> dict:
    """只针对已退市股票（universe.is_delisted=TRUE），补齐akshore/sina/eastmoney均无法
    取得的历史价格，缓解幸存者偏差（Phase 0.5评估文档项B的残留缺口）。"""
    conn = get_connection()
    init_schema(conn)
    where = "WHERE is_delisted = TRUE AND exchange IN ('sh','sz','bj')"
    params: list = []
    if symbols:
        placeholders = ",".join(["?"] * len(symbols))
        where += f" AND symbol IN ({placeholders})"
        params = list(symbols)
    rows = conn.execute(f"SELECT symbol, exchange FROM universe {where}", params).fetchall()
    conn.close()

    from common.tushare_client import to_ts_code

    targets = [(sym, to_ts_code(sym, exch)) for sym, exch in rows]

    return run_symbol_loop_sync(
        task_name="sync_delisted_daily_quotes",
        table="daily_quotes",
        key_cols=["symbol", "trade_date", "adjust"],
        fetch_fn=_fetch_delisted_daily,
        normalize_fn=_normalize_delisted_daily,
        targets=targets,
        limit=limit,
    )


def _fetch_listed_daily(ts_code: str, start_date: str) -> pd.DataFrame:
    return get_pro_api().daily(ts_code=ts_code, start_date=start_date)


def _raw_daily_to_qfq(daily: pd.DataFrame, adj: pd.DataFrame, symbol: str) -> pd.DataFrame:
    """Tushare daily 为未复权；用已落地的 adj_factor 换成与沪深库一致的 qfq。
    amount 从千元换成元，对齐新浪日线口径。"""
    if daily.empty:
        return pd.DataFrame()
    out = daily.rename(columns={"vol": "volume", "pct_chg": "pct_change"}).copy()
    out["trade_date"] = pd.to_datetime(out["trade_date"], format="%Y%m%d", errors="coerce").dt.date
    out = out.dropna(subset=["trade_date"])
    if adj.empty:
        logger.warning("%s 无复权因子，跳过 qfq 转换", symbol)
        return pd.DataFrame()
    adj = adj.copy()
    adj["trade_date"] = pd.to_datetime(adj["trade_date"], errors="coerce").dt.date
    merged = out.merge(adj[["trade_date", "adj_factor"]], on="trade_date", how="left")
    merged = merged.sort_values("trade_date")
    merged["adj_factor"] = merged["adj_factor"].ffill().bfill()
    if merged["adj_factor"].isna().all():
        logger.warning("%s 复权因子无法对齐日线，跳过", symbol)
        return pd.DataFrame()
    base = float(merged["adj_factor"].iloc[-1])
    if base == 0:
        return pd.DataFrame()
    ratio = merged["adj_factor"] / base
    for col in ("open", "high", "low", "close"):
        merged[col] = pd.to_numeric(merged[col], errors="coerce") * ratio
    merged["amount"] = pd.to_numeric(merged["amount"], errors="coerce") * 1000.0
    # 2026-08-26 修复：vol 单位是"手"，沪深库(sina源)是"股"，同表跨源必须统一单位，见上方
    # _normalize_delisted_daily 同一处修复的说明。
    merged["volume"] = pd.to_numeric(merged["volume"], errors="coerce") * 100.0
    merged["pct_change"] = merged["close"].pct_change() * 100.0
    merged["turnover"] = None
    merged["symbol"] = symbol
    merged["adjust"] = "qfq"
    return merged[
        ["symbol", "trade_date", "open", "high", "low", "close", "volume", "amount", "turnover", "pct_change", "adjust"]
    ].drop_duplicates(subset=["symbol", "trade_date", "adjust"])


def sync_bse_cdr_qfq_quotes(symbols: list[str] | None = None, limit: int | None = None) -> dict:
    """补东方财富失败留下的缺口：在市北交所日线 + 科创板 CDR 689009，写入 qfq。"""
    cfg_start = get_config()["ingestion"]["history_start_date"]
    conn = get_connection()
    init_schema(conn)
    if symbols:
        placeholders = ",".join(["?"] * len(symbols))
        rows = conn.execute(
            f"SELECT symbol, exchange FROM universe WHERE is_delisted = FALSE AND symbol IN ({placeholders})",
            list(symbols),
        ).fetchall()
    else:
        rows = conn.execute(
            """
            SELECT symbol, exchange FROM universe
            WHERE is_delisted = FALSE
              AND (exchange = 'bj' OR symbol = '689009')
            """
        ).fetchall()
    last_qfq = dict(
        conn.execute(
            "SELECT symbol, max(trade_date) FROM daily_quotes WHERE adjust = 'qfq' GROUP BY symbol"
        ).fetchall()
    )
    targets = [(sym, to_ts_code(sym, exch)) for sym, exch in rows]
    if limit:
        targets = targets[:limit]

    stats = {"ok": 0, "empty": 0, "failed": 0, "skipped_up_to_date": 0, "rows_written": 0, "failed_symbols": []}
    today = pd.Timestamp.today().strftime("%Y%m%d")
    fetch = retry_on_failure()(_fetch_listed_daily)

    for i, (symbol, ts_code) in enumerate(tqdm(targets, desc="sync_bse_cdr_qfq")):
        last = last_qfq.get(symbol)
        start = (pd.Timestamp(last) + pd.Timedelta(days=1)).strftime("%Y%m%d") if last else cfg_start
        if start > today:
            stats["skipped_up_to_date"] += 1
            continue
        try:
            raw = fetch(ts_code, start)
            adj = conn.execute(
                "SELECT trade_date, adj_factor FROM adj_factor WHERE symbol = ?",
                [symbol],
            ).df()
            if adj.empty:
                raw_adj = retry_on_failure()(_fetch_adj_factor)(ts_code)
                adj = _normalize_adj_factor(raw_adj, symbol)
                if not adj.empty:
                    upsert(conn, "adj_factor", ["symbol", "trade_date"], adj)
            norm = _raw_daily_to_qfq(raw, adj, symbol)
            if norm.empty:
                stats["empty"] += 1
            else:
                stats["rows_written"] += upsert(conn, "daily_quotes", ["symbol", "trade_date", "adjust"], norm)
                stats["ok"] += 1
        except Exception as exc:  # noqa: BLE001
            stats["failed"] += 1
            stats["failed_symbols"].append(symbol)
            logger.warning("sync_bse_cdr_qfq: %s 失败: %s: %s", symbol, type(exc).__name__, exc)
        if (i + 1) % 50 == 0:
            logger.info("sync_bse_cdr_qfq 进度 %d/%d: %s", i + 1, len(targets), stats)
        polite_sleep()

    conn.execute(
        "INSERT INTO sync_log (task_name, status, message) VALUES (?, ?, ?)",
        ["sync_bse_cdr_qfq", "success", str(stats)[:500]],
    )
    conn.close()
    logger.info("sync_bse_cdr_qfq 完成: %s", stats)
    return stats


def sync_all_tushare_prices() -> dict:
    results = {}
    logger.info("=== Tushare 复权因子 + 退市股历史行情 开始 ===")
    results["adj_factor"] = sync_adj_factor()
    results["delisted_daily_quotes"] = sync_delisted_daily_quotes()
    results["bse_cdr_qfq_quotes"] = sync_bse_cdr_qfq_quotes()
    logger.info("=== Tushare 复权因子 + 退市股历史行情 完成: %s ===", results)
    return results


if __name__ == "__main__":
    import argparse
    import os

    from common.db import sidecar_parallel_active

    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    if sidecar_parallel_active() and not os.environ.get("STOCK_QUANT_DB"):
        print("SKIP: Tushare 已在旁路库并行抓取，主库链跳过此步")
        raise SystemExit(0)
    parser = argparse.ArgumentParser(description="Tushare复权因子+退市股历史行情批量抓取")
    parser.add_argument("--task", choices=["all", "adj_factor", "delisted", "bse_cdr"], default="all")
    parser.add_argument("--limit", type=int, default=None)
    args = parser.parse_args()

    if args.task == "all":
        print(sync_all_tushare_prices())
    elif args.task == "adj_factor":
        print(sync_adj_factor(limit=args.limit))
    elif args.task == "delisted":
        print(sync_delisted_daily_quotes(limit=args.limit))
    elif args.task == "bse_cdr":
        print(sync_bse_cdr_qfq_quotes(limit=args.limit))
