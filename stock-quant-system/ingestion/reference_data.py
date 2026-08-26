"""
Phase 0.5 数据缺口补齐：参考/静态数据抓取模块。

三个子任务均为"全市场少量请求覆盖"设计，不是逐股票循环，预期耗时是分钟级
（区别于 quotes_batch/fundamentals_batch 的逐股票小时级批处理）：

    1. sync_disclosure_calendar()：财务公告日历（point-in-time），解决"用未公告
       数据训练/回测"的未来函数风险。约44次请求（history_start_year~今年 × 4季度），
       每次覆盖全市场一个报告期。
    2. sync_delisted_universe()：沪深退市股清单，缓解幸存者偏差。仅2次请求
       （价格历史仍是已知缺口，见 docs/phase0.5-gap-closing-assessment.md）。
    3. sync_industry_classification()：申万宏源行业分类变动历史，供 Phase 1
       行业中性化使用。仅1次请求，覆盖全市场且天然point-in-time。

必要性与可实现性评估过程见 docs/phase0.5-gap-closing-assessment.md。
"""
from __future__ import annotations

import datetime as dt
import io
import logging

import akshare as ak
import pandas as pd
import requests
import urllib3

from common.config import get_config
from common.db import get_connection, init_schema
from common.http_retry import FetchFailedError, polite_sleep, retry_on_failure
from ingestion.universe import classify_symbol

logger = logging.getLogger("ingestion.reference_data")

# swsresearch.com 证书链在本机网络环境下校验失败（CERTIFICATE_VERIFY_FAILED），
# 经核实是该研究站点自身证书链的已知问题（非本项目要访问的敏感接口），实测数据可正常
# 拿到（200 + 正确的Excel内容），用 verify=False 绕过。仅用于这一处公开分类文件下载。
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)


# ---------------------------------------------------------------------------
# 1. 财务公告日历（point-in-time）
# ---------------------------------------------------------------------------

_PERIOD_SUFFIXES = [("一季", "03-31"), ("半年报", "06-30"), ("三季", "09-30"), ("年报", "12-31")]


@retry_on_failure()
def _fetch_disclosure(market: str, period: str) -> pd.DataFrame:
    return ak.stock_report_disclosure(market=market, period=period)


def _iter_periods(start_year: int, end_year: int):
    today = dt.date.today()
    for year in range(start_year, end_year + 1):
        for suffix, month_day in _PERIOD_SUFFIXES:
            report_date = dt.date.fromisoformat(f"{year}-{month_day}")
            if report_date > today:
                continue
            yield f"{year}{suffix}", report_date


def sync_disclosure_calendar(start_year: int | None = None, end_year: int | None = None) -> dict:
    """全市场财务公告日历批量抓取：每个报告期一次请求覆盖全市场，非逐股票循环。"""
    cfg = get_config()
    start_year = start_year or int(cfg["ingestion"]["history_start_date"][:4])
    end_year = end_year or dt.date.today().year

    conn = get_connection()
    init_schema(conn)
    total_rows = 0
    periods_done = 0
    periods_failed: list[str] = []
    try:
        for period_label, report_date in _iter_periods(start_year, end_year):
            try:
                raw = _fetch_disclosure(market="沪深京", period=period_label)
            except FetchFailedError as exc:
                periods_failed.append(period_label)
                conn.execute(
                    "INSERT INTO sync_log (task_name, symbol, status, message) VALUES (?, ?, ?, ?)",
                    ["sync_disclosure_calendar", None, "failed", f"{period_label}: {str(exc)[:200]}"],
                )
                logger.warning("公告日历 %s 抓取失败，跳过: %s", period_label, exc)
                polite_sleep()
                continue

            df = raw.rename(columns={"股票代码": "symbol", "实际披露": "announce_date"})
            df["symbol"] = df["symbol"].astype(str).str.zfill(6)
            df["announce_date"] = pd.to_datetime(df["announce_date"], errors="coerce").dt.date
            df["report_date"] = report_date
            df = df[["symbol", "report_date", "announce_date"]].drop_duplicates(subset=["symbol", "report_date"])

            conn.register("incoming_disclosure", df)
            conn.execute(
                """
                DELETE FROM disclosure_calendar
                WHERE (symbol, report_date) IN (SELECT symbol, report_date FROM incoming_disclosure)
                """
            )
            conn.execute(
                """
                INSERT INTO disclosure_calendar (symbol, report_date, announce_date)
                SELECT symbol, report_date, announce_date FROM incoming_disclosure
                """
            )
            conn.unregister("incoming_disclosure")

            total_rows += len(df)
            periods_done += 1
            logger.info("公告日历 %s(%s) 完成: %d条", period_label, report_date, len(df))
            polite_sleep()

        conn.execute(
            "INSERT INTO sync_log (task_name, status, message) VALUES (?, ?, ?)",
            [
                "sync_disclosure_calendar",
                "success",
                f"periods_done={periods_done}, rows={total_rows}, failed={periods_failed}",
            ],
        )
        logger.info(
            "批量公告日历同步完成: periods=%d rows=%d failed=%s", periods_done, total_rows, periods_failed
        )
        return {"periods_done": periods_done, "rows": total_rows, "failed_periods": periods_failed}
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# 2. 退市股清单（缓解幸存者偏差）
# ---------------------------------------------------------------------------


@retry_on_failure()
def _fetch_sh_delist() -> pd.DataFrame:
    return ak.stock_info_sh_delist(symbol="全部")


@retry_on_failure()
def _fetch_sz_delist() -> pd.DataFrame:
    return ak.stock_info_sz_delist(symbol="终止上市公司")


def sync_delisted_universe() -> dict:
    """抓取沪深退市股清单，把准确的上市/退市日期回填/新增到 universe 表。

    已知限制：这里只补齐"哪些股票在历史上存在过、何时退市"这一事实，
    不解决退市股的历史价格数据缺口（sina/东财均无法取得，见评估文档）。
    """
    sh = _fetch_sh_delist().rename(
        columns={"公司代码": "symbol", "公司简称": "name", "上市日期": "list_date", "暂停上市日期": "delist_date"}
    )
    polite_sleep()
    sz = _fetch_sz_delist().rename(
        columns={"证券代码": "symbol", "证券简称": "name", "上市日期": "list_date", "终止上市日期": "delist_date"}
    )

    combined = pd.concat(
        [sh[["symbol", "name", "list_date", "delist_date"]], sz[["symbol", "name", "list_date", "delist_date"]]],
        ignore_index=True,
    )
    combined["symbol"] = combined["symbol"].astype(str).str.zfill(6)
    combined["list_date"] = pd.to_datetime(combined["list_date"], errors="coerce").dt.date
    combined["delist_date"] = pd.to_datetime(combined["delist_date"], errors="coerce").dt.date
    combined = combined.dropna(subset=["symbol"]).drop_duplicates(subset=["symbol"], keep="last")

    exch_board = combined["symbol"].map(classify_symbol)
    combined["exchange"] = exch_board.map(lambda t: t[0])
    combined["board"] = exch_board.map(lambda t: t[1])

    conn = get_connection()
    init_schema(conn)
    try:
        conn.register("incoming_delisted", combined)

        conn.execute(
            """
            UPDATE universe AS u
            SET list_date = i.list_date,
                delist_date = i.delist_date,
                is_delisted = TRUE,
                updated_at = current_timestamp
            FROM incoming_delisted AS i
            WHERE u.symbol = i.symbol
            """
        )

        conn.execute(
            """
            INSERT INTO universe
                (symbol, exchange, board, name, is_st, is_delisted, first_seen_date, last_seen_date, list_date, delist_date)
            SELECT i.symbol, i.exchange, i.board, i.name, FALSE, TRUE, i.list_date, i.delist_date, i.list_date, i.delist_date
            FROM incoming_delisted AS i
            WHERE NOT EXISTS (SELECT 1 FROM universe u WHERE u.symbol = i.symbol)
            """
        )
        conn.unregister("incoming_delisted")

        n_with_delist_date = conn.execute(
            "SELECT count(*) FROM universe WHERE delist_date IS NOT NULL"
        ).fetchone()[0]

        conn.execute(
            "INSERT INTO sync_log (task_name, status, message) VALUES (?, ?, ?)",
            [
                "sync_delisted_universe",
                "success",
                f"delisted_list={len(combined)}, universe_with_delist_date={n_with_delist_date}",
            ],
        )
        logger.info(
            "退市股清单同步完成: 清单%d只, universe中已有精确退市日期%d只", len(combined), n_with_delist_date
        )
        return {"delisted_list": len(combined), "universe_with_delist_date": n_with_delist_date}
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# 3. 行业分类（申万宏源，变动历史，point-in-time）
# ---------------------------------------------------------------------------

_SW_INDUSTRY_URL = "https://www.swsresearch.com/swindex/pdf/SwClass2021/StockClassifyUse_stock.xls"


def _fetch_sw_industry_hist_raw() -> pd.DataFrame:
    headers = {"User-Agent": "Mozilla/5.0"}
    resp = requests.get(_SW_INDUSTRY_URL, headers=headers, verify=False, timeout=60)
    resp.raise_for_status()
    return pd.read_excel(io.BytesIO(resp.content), dtype={"股票代码": "str", "行业代码": "str"})


_fetch_sw_industry_hist = retry_on_failure()(_fetch_sw_industry_hist_raw)


def sync_industry_classification() -> dict:
    """申万宏源官方行业分类变动历史，一次请求覆盖全市场，供 Phase 1 行业中性化使用。

    已知限制：行业代码与 sw_index_first/second/third_info 的编码体系不是同一套，
    人类可读行业名称解码留待后续按需补充；本表只保证"编码分组"可用于中性化计算。
    """
    raw = _fetch_sw_industry_hist()
    raw = raw.rename(
        columns={"股票代码": "symbol", "计入日期": "start_date", "行业代码": "industry_code", "更新日期": "source_updated_at"}
    )
    raw["symbol"] = raw["symbol"].astype(str).str.zfill(6)
    raw["start_date"] = pd.to_datetime(raw["start_date"], errors="coerce").dt.date
    raw["source_updated_at"] = pd.to_datetime(raw["source_updated_at"], errors="coerce").dt.date
    raw = raw.dropna(subset=["symbol", "start_date"])
    raw = raw[["symbol", "start_date", "industry_code", "source_updated_at"]]
    raw = raw.drop_duplicates(subset=["symbol", "start_date"], keep="last")

    conn = get_connection()
    init_schema(conn)
    try:
        conn.register("incoming_industry", raw)
        conn.execute(
            """
            DELETE FROM industry_classification
            WHERE (symbol, start_date) IN (SELECT symbol, start_date FROM incoming_industry)
            """
        )
        conn.execute(
            """
            INSERT INTO industry_classification (symbol, start_date, industry_code, source_updated_at)
            SELECT symbol, start_date, industry_code, source_updated_at FROM incoming_industry
            """
        )
        conn.unregister("incoming_industry")

        n_rows = len(raw)
        n_symbols = int(raw["symbol"].nunique())
        conn.execute(
            "INSERT INTO sync_log (task_name, status, message) VALUES (?, ?, ?)",
            ["sync_industry_classification", "success", f"rows={n_rows}, symbols={n_symbols}"],
        )
        logger.info("行业分类同步完成: %d条记录, 覆盖%d只股票", n_rows, n_symbols)
        return {"rows": n_rows, "symbols": n_symbols}
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# 编排入口
# ---------------------------------------------------------------------------


def sync_all_reference_data() -> dict:
    results = {}
    logger.info("=== Phase 0.5 参考数据补齐开始 ===")
    results["disclosure_calendar"] = sync_disclosure_calendar()
    results["delisted_universe"] = sync_delisted_universe()
    results["industry_classification"] = sync_industry_classification()
    logger.info("=== Phase 0.5 参考数据补齐完成: %s ===", results)
    return results


if __name__ == "__main__":
    import argparse

    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    parser = argparse.ArgumentParser(description="Phase 0.5 参考数据补齐")
    parser.add_argument(
        "--task",
        choices=["all", "disclosure", "delisted", "industry"],
        default="all",
        help="只运行指定子任务，默认all",
    )
    args = parser.parse_args()

    if args.task == "all":
        print(sync_all_reference_data())
    elif args.task == "disclosure":
        print(sync_disclosure_calendar())
    elif args.task == "delisted":
        print(sync_delisted_universe())
    elif args.task == "industry":
        print(sync_industry_classification())
