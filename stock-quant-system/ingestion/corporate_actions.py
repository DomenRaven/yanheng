"""
Phase 0.6 数据缺口补齐（第二轮）—— 重量批次一：股本变动历史 + 分红配股历史。

数据源：巨潮资讯（cninfo），与 Phase 0.5 disclosure_calendar 同源，逐股票抓取，
每只股票2次请求（股本变动 + 分红），合并成同一个循环以减少总耗时
（约5500只 × 2次请求，预计3-5小时，与 fundamentals_batch 同量级）。

用途：
  - share_changes.total_shares/circulating_shares 是市值/规模因子的基础输入，
    与 daily_quotes.close 相乘即可得到 point-in-time 市值（需按 announce_date 对齐）。
  - dividends.ex_date 用于识别"复权价格跳变但非涨跌停"的场景，避免回测引擎
    在除权日误判涨跌停锁定状态（见 docs/phase0.6-secondary-gap-assessment.md 项S）。

已知限制：两个接口均不提供"是否已是最新"的增量标记，采用与 fundamentals_batch
一致的"重新拉全历史再按公告日去重覆盖"策略，而非增量判断（数据量小，全量重拉成本可接受）。
"""
from __future__ import annotations

import logging

import akshare as ak
import pandas as pd

from common.config import get_config
from common.db import write_session
from common.http_retry import default_limiter, retry_on_failure
from common.parallel_fetch import map_fetch_then_write

logger = logging.getLogger("ingestion.corporate_actions")


@retry_on_failure()
def _fetch_share_change(symbol: str) -> pd.DataFrame:
    try:
        return ak.stock_share_change_cninfo(symbol=symbol)
    except KeyError as exc:
        # 2026-08-26 查证：akshare stock_share_change_cninfo 在 cninfo 对该股票返回
        # 空结果集时，仍无条件访问重命名后的"公告日期"列，抛出 KeyError('公告日期')。
        # 158只股票复现均是这个模式（akshare库自身的健壮性缺陷，非网络问题），
        # 重试5次也不会变好——直接当作"该股票无股本变动记录"处理，不再当失败重试。
        if str(exc).strip("'\"") == "公告日期":
            logger.info("%s 股本变动接口返回空结果（akshare已知KeyError），按无记录处理", symbol)
            return pd.DataFrame()
        raise


@retry_on_failure()
def _fetch_dividend(symbol: str) -> pd.DataFrame:
    try:
        return ak.stock_dividend_cninfo(symbol=symbol)
    except KeyError as exc:
        # 同 _fetch_share_change 的akshare已知缺陷：cninfo空结果时该列不存在，
        # akshare未做空结果判断直接访问，抛KeyError('实施方案公告日期')。
        if str(exc).strip("'\"") == "实施方案公告日期":
            logger.info("%s 分红历史接口返回空结果（akshare已知KeyError），按无记录处理", symbol)
            return pd.DataFrame()
        raise


def _normalize_share_change(df: pd.DataFrame, symbol: str) -> pd.DataFrame:
    if df.empty:
        return df
    df = df.rename(
        columns={
            "变动日期": "change_date",
            "公告日期": "announce_date",
            "总股本": "total_shares",
            "已流通股份": "circulating_shares",
            "变动原因": "change_reason",
        }
    )
    keep = ["change_date", "announce_date", "total_shares", "circulating_shares", "change_reason"]
    for col in keep:
        if col not in df.columns:
            df[col] = None
    df = df[keep].copy()
    df["symbol"] = symbol
    df["change_date"] = pd.to_datetime(df["change_date"], errors="coerce").dt.date
    df["announce_date"] = pd.to_datetime(df["announce_date"], errors="coerce").dt.date
    df = df.dropna(subset=["change_date"])
    return df[["symbol", "change_date", "announce_date", "total_shares", "circulating_shares", "change_reason"]]


def _normalize_dividend(df: pd.DataFrame, symbol: str) -> pd.DataFrame:
    if df.empty:
        return df
    df = df.rename(
        columns={
            "实施方案公告日期": "plan_announce_date",
            "分红类型": "dividend_type",
            "送股比例": "bonus_ratio",
            "转增比例": "transfer_ratio",
            "派息比例": "cash_ratio",
            "股权登记日": "record_date",
            "除权日": "ex_date",
            "派息日": "payment_date",
            "报告时间": "report_period",
        }
    )
    keep = [
        "plan_announce_date", "dividend_type", "bonus_ratio", "transfer_ratio",
        "cash_ratio", "record_date", "ex_date", "payment_date", "report_period",
    ]
    for col in keep:
        if col not in df.columns:
            df[col] = None
    df = df[keep].copy()
    df["symbol"] = symbol
    for date_col in ["plan_announce_date", "record_date", "ex_date", "payment_date"]:
        df[date_col] = pd.to_datetime(df[date_col], errors="coerce").dt.date
    df = df.dropna(subset=["plan_announce_date"])
    return df[
        ["symbol", "plan_announce_date", "dividend_type", "bonus_ratio", "transfer_ratio",
         "cash_ratio", "record_date", "ex_date", "payment_date", "report_period"]
    ]


def _upsert(conn, table: str, key_cols: list[str], df: pd.DataFrame) -> int:
    if df.empty:
        return 0
    # 2026-08-26 修复：源数据本身可能同一天有多条记录（实测000007在1993-05-16有两条
    # 分红公告），DELETE只处理"与库里已有行"的冲突，不处理"本批次内部"的重复，
    # 未去重直接INSERT会触发主键冲突并让整批任务崩掉。保留同一key的最后一条。
    df = df.drop_duplicates(subset=key_cols, keep="last")
    conn.register("incoming_ca", df)
    key_tuple = ", ".join(key_cols)
    conn.execute(
        f"""
        DELETE FROM {table}
        WHERE ({key_tuple}) IN (SELECT {key_tuple} FROM incoming_ca)
        """
    )
    cols = ", ".join(df.columns)
    conn.execute(f"INSERT INTO {table} ({cols}) SELECT {cols} FROM incoming_ca")
    conn.unregister("incoming_ca")
    return len(df)


def _get_targets(conn, symbols: list[str] | None) -> list[str]:
    where = "WHERE is_delisted = FALSE AND exchange IN ('sh','sz')"
    params: list = []
    if symbols:
        placeholders = ",".join(["?"] * len(symbols))
        where += f" AND symbol IN ({placeholders})"
        params = list(symbols)
    rows = conn.execute(f"SELECT symbol FROM universe {where}", params).fetchall()
    return [r[0] for r in rows]


def sync_corporate_actions(symbols: list[str] | None = None, limit: int | None = None) -> dict:
    checkpoint_size = get_config()["ingestion"].get("checkpoint_batch_size", 50)

    with write_session(init=True) as conn:
        targets = _get_targets(conn, symbols)
    if limit:
        targets = targets[:limit]

    stats = {
        "share_ok": 0, "share_empty": 0, "share_failed": 0, "share_rows": 0,
        "div_ok": 0, "div_empty": 0, "div_failed": 0, "div_rows": 0,
        "failed_symbols": [],
    }
    done = {"n": 0}

    def fetch(symbol: str) -> tuple[pd.DataFrame, pd.DataFrame]:
        share_df = _normalize_share_change(_fetch_share_change(symbol), symbol)
        default_limiter().wait()
        div_df = _normalize_dividend(_fetch_dividend(symbol), symbol)
        return share_df, div_df

    def on_result(
        symbol: str,
        pair: tuple[pd.DataFrame, pd.DataFrame] | None,
        err: BaseException | None,
    ) -> None:
        done["n"] += 1
        if err is not None:
            stats["share_failed"] += 1
            stats["div_failed"] += 1
            stats["failed_symbols"].append(symbol)
            logger.warning("%s 公司行为抓取失败: %s: %s", symbol, type(err).__name__, err)
            with write_session() as conn:
                conn.execute(
                    "INSERT INTO sync_log (task_name, symbol, status, message) VALUES (?, ?, ?, ?)",
                    ["sync_corporate_actions", symbol, "failed", f"{type(err).__name__}: {str(err)[:280]}"],
                )
            return
        if pair is None:
            stats["share_failed"] += 1
            stats["div_failed"] += 1
            stats["failed_symbols"].append(symbol)
            return
        share_df, div_df = pair
        with write_session() as conn:
            if share_df.empty:
                stats["share_empty"] += 1
            else:
                stats["share_rows"] += _upsert(conn, "share_changes", ["symbol", "change_date"], share_df)
                stats["share_ok"] += 1
            if div_df.empty:
                stats["div_empty"] += 1
            else:
                stats["div_rows"] += _upsert(conn, "dividends", ["symbol", "plan_announce_date"], div_df)
                stats["div_ok"] += 1
        if done["n"] % checkpoint_size == 0:
            logger.info("进度 %d/%d: %s", done["n"], len(targets), stats)

    map_fetch_then_write(targets, fetch, on_result, desc="corporate_actions")
    with write_session() as conn:
        conn.execute(
            "INSERT INTO sync_log (task_name, status, message) VALUES (?, ?, ?)",
            ["sync_corporate_actions", "success", str(stats)[:500]],
        )
    logger.info("公司行为(股本变动+分红)批量同步完成: %s", stats)
    return stats


if __name__ == "__main__":
    import argparse

    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    parser = argparse.ArgumentParser(description="全市场股本变动+分红历史批量抓取")
    parser.add_argument("--limit", type=int, default=None, help="仅同步前N只股票（调试用）")
    parser.add_argument("--symbols", type=str, default=None, help="逗号分隔的股票代码列表")
    args = parser.parse_args()

    symbols = args.symbols.split(",") if args.symbols else None
    result = sync_corporate_actions(symbols=symbols, limit=args.limit)
    print(result)
