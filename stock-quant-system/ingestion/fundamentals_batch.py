"""
全市场基础财务指标批量/增量抓取。

数据源：ak.stock_financial_analysis_indicator（新浪财经），按股票逐个抓取，
返回每季度 80+ 项财务比率指标（每股收益/ROE/各类周转率/资产负债结构等）。
落地为 EAV 长表（见 common/db.py 的表结构注释）。

增量逻辑：对每只股票，查询 fundamentals 中已有的最大 report_date；
若数据源返回的最新报告期不比已有的更新，则跳过（避免重复抓取全历史）。
财务数据更新频率低（季度），因此不做"按天增量"，而是"按是否有新报告期"判断。

已知限制：该接口不提供公告日，Phase 1 使用此表做因子回测前需先处理
point-in-time 对齐问题（见表结构注释），不能直接假设报告期当天数据已公开。

2026-08-26 补北交所缺口：新浪该接口不支持北交所代码，338只活跃北交所股票在本表
里此前是0覆盖（Phase 0 验收发现，见 docs/phase0-acceptance-report.md 缺口1）。
用 Tushare `fina_indicator`（非VIP，2000积分档可用）补一个"质量因子必需最小集"
（15项，ROE/ROA/毛利率/净利率/资产负债率/流动速动比率/周转率/每股净资产/增长率），
不追求与新浪80项全量字段对齐——性价比不高，且部分新浪指标（如各账期应收款分布）
在Tushare里没有等价字段。字段名映射到与新浪完全相同的中文indicator字符串，
使两个数据源在同一张EAV表里可以无缝混用（Phase 1 pivot时不用区分数据来源）。
"""
from __future__ import annotations

import datetime as dt
import logging

import akshare as ak
import pandas as pd
from tqdm import tqdm

from common.config import get_config
from common.db import get_connection, init_schema
from common.http_retry import FetchFailedError, polite_sleep, retry_on_failure
from common.tushare_client import get_pro_api, to_ts_code

logger = logging.getLogger("ingestion.fundamentals_batch")

# Tushare fina_indicator 字段 -> 新浪 stock_financial_analysis_indicator 同义中文列名
# 只映射"质量因子必需最小集"，不追求80项全量对齐（见模块docstring）
_TS_FINA_FIELD_MAP = {
    "dt_eps": "摊薄每股收益(元)",
    "roe": "净资产收益率(%)",
    "roe_waa": "加权净资产收益率(%)",
    "roa": "总资产净利润率(%)",
    "grossprofit_margin": "销售毛利率(%)",
    "netprofit_margin": "销售净利率(%)",
    "debt_to_assets": "资产负债率(%)",
    "current_ratio": "流动比率",
    "quick_ratio": "速动比率",
    "assets_turn": "总资产周转率(次)",
    "ar_turn": "应收账款周转率(次)",
    "bps": "每股净资产_调整后(元)",
    "netprofit_yoy": "净利润增长率(%)",
    "or_yoy": "主营业务收入增长率(%)",
    "equity_yoy": "净资产增长率(%)",
}
_TS_FINA_FIELDS = ["ts_code", "end_date"] + list(_TS_FINA_FIELD_MAP.keys())


@retry_on_failure()
def _fetch_financial_indicator(symbol: str, start_year: str) -> pd.DataFrame:
    return ak.stock_financial_analysis_indicator(symbol=symbol, start_year=start_year)


def _to_long_format(df: pd.DataFrame, symbol: str) -> pd.DataFrame:
    df = df.rename(columns={"日期": "report_date"})
    df["report_date"] = pd.to_datetime(df["report_date"], errors="coerce")
    df = df.dropna(subset=["report_date"])
    value_cols = [c for c in df.columns if c != "report_date"]
    long_df = df.melt(id_vars=["report_date"], value_vars=value_cols, var_name="indicator", value_name="value")
    long_df["symbol"] = symbol
    long_df = long_df.dropna(subset=["value"])
    return long_df[["symbol", "report_date", "indicator", "value"]]


def _get_sync_targets(conn, symbols: list[str] | None) -> list[tuple[str, dt.date | None]]:
    where = "WHERE is_delisted = FALSE AND exchange IN ('sh','sz')"
    params: list = []
    if symbols:
        placeholders = ",".join(["?"] * len(symbols))
        where += f" AND symbol IN ({placeholders})"
        params = list(symbols)

    universe_rows = conn.execute(f"SELECT symbol FROM universe {where}", params).fetchall()
    last_dates = dict(
        conn.execute("SELECT symbol, max(report_date) FROM fundamentals GROUP BY symbol").fetchall()
    )
    return [(sym, last_dates.get(sym)) for (sym,) in universe_rows]


def _upsert_fundamentals(conn, df: pd.DataFrame) -> int:
    if df.empty:
        return 0
    conn.register("incoming_fundamentals", df)
    conn.execute(
        """
        DELETE FROM fundamentals
        WHERE (symbol, report_date, indicator) IN (
            SELECT symbol, report_date, indicator FROM incoming_fundamentals
        )
        """
    )
    conn.execute(
        """
        INSERT INTO fundamentals (symbol, report_date, indicator, value)
        SELECT symbol, report_date, indicator, value FROM incoming_fundamentals
        """
    )
    conn.unregister("incoming_fundamentals")
    return len(df)


def sync_fundamentals_batch(
    symbols: list[str] | None = None,
    limit: int | None = None,
    skip_existing: bool = False,
) -> dict:
    """skip_existing=True：已有任意财务记录的股票直接跳过（不打接口、不sleep）。
    专用于全量首跑中断后的快速续传；日常增量更新请保持 False，以便检查是否有新报告期。"""
    history_start_year = get_config()["ingestion"]["history_start_date"][:4]
    checkpoint_size = get_config()["ingestion"].get("checkpoint_batch_size", 50)

    conn = get_connection()
    init_schema(conn)

    targets = _get_sync_targets(conn, symbols)
    if skip_existing:
        before = len(targets)
        targets = [(sym, last) for sym, last in targets if last is None]
        logger.info(
            "skip_existing=True：跳过已有财务数据的股票 %d 只，剩余待抓 %d 只",
            before - len(targets),
            len(targets),
        )
    if limit:
        targets = targets[:limit]

    stats = {
        "ok": 0,
        "no_new_data": 0,
        "skipped_existing": 0,
        "failed": 0,
        "rows_written": 0,
        "failed_symbols": [],
    }

    for i, (symbol, last_report_date) in enumerate(tqdm(targets, desc="fundamentals_batch")):
        start_year = str(last_report_date.year) if last_report_date else history_start_year
        try:
            raw = _fetch_financial_indicator(symbol, start_year)
            long_df = _to_long_format(raw, symbol)
            if last_report_date:
                long_df = long_df[long_df["report_date"] > pd.Timestamp(last_report_date)]

            if long_df.empty:
                stats["no_new_data"] += 1
            else:
                n = _upsert_fundamentals(conn, long_df)
                stats["rows_written"] += n
                stats["ok"] += 1

            conn.execute(
                "INSERT INTO sync_log (task_name, symbol, status, message) VALUES (?, ?, ?, ?)",
                ["sync_fundamentals_batch", symbol, "success", f"rows={len(long_df)}"],
            )
        except FetchFailedError as exc:
            stats["failed"] += 1
            stats["failed_symbols"].append(symbol)
            conn.execute(
                "INSERT INTO sync_log (task_name, symbol, status, message) VALUES (?, ?, ?, ?)",
                ["sync_fundamentals_batch", symbol, "failed", str(exc)[:300]],
            )
            logger.warning("%s 财务数据抓取失败，已记录，跳过: %s", symbol, exc)

        if (i + 1) % checkpoint_size == 0:
            logger.info(
                "进度 %d/%d: ok=%d no_new=%d failed=%d rows=%d",
                i + 1, len(targets), stats["ok"], stats["no_new_data"], stats["failed"], stats["rows_written"],
            )

        polite_sleep()

    conn.close()
    logger.info("批量财务数据同步完成: %s", stats)
    return stats


@retry_on_failure()
def _fetch_fina_indicator_tushare(ts_code: str) -> pd.DataFrame:
    return get_pro_api().fina_indicator(ts_code=ts_code, fields=_TS_FINA_FIELDS)


def _tushare_fina_to_long(df: pd.DataFrame, symbol: str) -> pd.DataFrame:
    if df.empty or "end_date" not in df.columns:
        return pd.DataFrame(columns=["symbol", "report_date", "indicator", "value"])
    out = df.copy()
    out["report_date"] = pd.to_datetime(out["end_date"], format="%Y%m%d", errors="coerce")
    out = out.dropna(subset=["report_date"])
    out = out.drop_duplicates(subset=["end_date"], keep="last")
    value_cols = list(_TS_FINA_FIELD_MAP.keys())
    long_df = out.melt(id_vars=["report_date"], value_vars=value_cols, var_name="indicator", value_name="value")
    long_df["indicator"] = long_df["indicator"].map(_TS_FINA_FIELD_MAP)
    long_df["value"] = pd.to_numeric(long_df["value"], errors="coerce")
    long_df = long_df.dropna(subset=["value"])
    long_df["report_date"] = long_df["report_date"].dt.date
    long_df["symbol"] = symbol
    return long_df[["symbol", "report_date", "indicator", "value"]].drop_duplicates(
        subset=["symbol", "report_date", "indicator"]
    )


def sync_fundamentals_bse_tushare(symbols: list[str] | None = None, limit: int | None = None) -> dict:
    """补北交所缺口（见模块docstring）：Tushare fina_indicator -> fundamentals 同一张表。"""
    conn = get_connection()
    init_schema(conn)
    where = "WHERE is_delisted = FALSE AND exchange = 'bj'"
    params: list = []
    if symbols:
        placeholders = ",".join(["?"] * len(symbols))
        where += f" AND symbol IN ({placeholders})"
        params = list(symbols)
    rows = conn.execute(f"SELECT symbol, exchange FROM universe {where}", params).fetchall()
    targets = [(sym, to_ts_code(sym, exch)) for sym, exch in rows]
    if limit:
        targets = targets[:limit]

    stats = {"ok": 0, "empty": 0, "failed": 0, "rows_written": 0, "failed_symbols": []}
    for i, (symbol, ts_code) in enumerate(tqdm(targets, desc="sync_fundamentals_bse_tushare")):
        try:
            raw = _fetch_fina_indicator_tushare(ts_code)
            long_df = _tushare_fina_to_long(raw, symbol)
            if long_df.empty:
                stats["empty"] += 1
            else:
                stats["rows_written"] += _upsert_fundamentals(conn, long_df)
                stats["ok"] += 1
            conn.execute(
                "INSERT INTO sync_log (task_name, symbol, status, message) VALUES (?, ?, ?, ?)",
                ["sync_fundamentals_bse_tushare", symbol, "success", f"rows={len(long_df)}"],
            )
        except Exception as exc:  # noqa: BLE001
            stats["failed"] += 1
            stats["failed_symbols"].append(symbol)
            conn.execute(
                "INSERT INTO sync_log (task_name, symbol, status, message) VALUES (?, ?, ?, ?)",
                ["sync_fundamentals_bse_tushare", symbol, "failed", f"{type(exc).__name__}: {str(exc)[:280]}"],
            )
            logger.warning("%s 北交所fundamentals(Tushare)抓取失败: %s", symbol, exc)

        if (i + 1) % 50 == 0:
            logger.info("sync_fundamentals_bse_tushare 进度 %d/%d: %s", i + 1, len(targets), stats)
        polite_sleep()

    conn.execute(
        "INSERT INTO sync_log (task_name, status, message) VALUES (?, ?, ?)",
        ["sync_fundamentals_bse_tushare", "success", str(stats)[:500]],
    )
    conn.close()
    logger.info("sync_fundamentals_bse_tushare 完成: %s", stats)
    return stats


if __name__ == "__main__":
    import argparse

    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    parser = argparse.ArgumentParser(description="全市场基础财务指标批量/增量抓取")
    parser.add_argument("--limit", type=int, default=None, help="仅同步前N只股票（调试用）")
    parser.add_argument("--symbols", type=str, default=None, help="逗号分隔的股票代码列表，仅同步指定股票")
    parser.add_argument(
        "--skip-existing",
        action="store_true",
        help="已有财务记录的股票直接跳过（全量首跑中断后快速续传用；日常增量不要开）",
    )
    parser.add_argument(
        "--task",
        choices=["sina", "bse_tushare"],
        default="sina",
        help="sina=沪深新浪比率指标（默认）；bse_tushare=北交所Tushare补齐",
    )
    args = parser.parse_args()

    symbols = args.symbols.split(",") if args.symbols else None
    if args.task == "bse_tushare":
        result = sync_fundamentals_bse_tushare(symbols=symbols, limit=args.limit)
    else:
        result = sync_fundamentals_batch(
            symbols=symbols, limit=args.limit, skip_existing=args.skip_existing
        )
    print(result)
