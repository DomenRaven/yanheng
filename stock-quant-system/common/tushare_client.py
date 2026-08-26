"""
Tushare Pro 共用客户端与通用批处理辅助函数。

背景：2026-08-25 用户升级到 Tushare Pro 2000积分档位（200元/年），解锁了本项目此前
两轮评估文档记录的多项已知缺口所需接口（adj_factor/daily_basic/index_weight/suspend_d
/stk_limit）以及 Phase 2 行为金融层数据源（top_list/moneyflow/margin/pledge_stat/
stk_holdernumber/block_trade）。2000档限频 200次/分钟、100000次/天/接口，足够覆盖
全市场~5500只股票或~10年交易日的批量抓取需求。

token 存放在项目根目录 .env（TUSHARE_TOKEN=...），不硬编码进代码，.env 已在 .gitignore。

两类批处理模式：
    - "逐股票"：每只股票一次调用返回其全部历史（如 adj_factor/daily_basic），
      适用于本身按 ts_code 查询的接口。
    - "逐交易日"：每个交易日一次调用返回全市场当日快照（如 suspend_d/stk_limit/
      top_list），适用于本身是"市场快照"类的接口，比逐股票循环效率高得多。
"""
from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Callable

import pandas as pd
import tushare as ts
from tqdm import tqdm

from common.config import get_config
from common.db import get_connection, init_schema
from common.http_retry import FetchFailedError, polite_sleep, retry_on_failure

logger = logging.getLogger("ingestion.tushare")

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def _load_token() -> str:
    env_path = PROJECT_ROOT / ".env"
    if env_path.exists():
        with open(env_path, "r", encoding="utf-8") as f:
            for line in f:
                if line.startswith("TUSHARE_TOKEN="):
                    return line.strip().split("=", 1)[1]
    token = os.environ.get("TUSHARE_TOKEN")
    if token:
        return token
    raise RuntimeError("未找到 TUSHARE_TOKEN，请在项目根目录 .env 中配置 TUSHARE_TOKEN=xxx")


_pro = None


def get_pro_api():
    global _pro
    if _pro is None:
        _pro = ts.pro_api(_load_token())
    return _pro


def to_ts_code(symbol: str, exchange: str) -> str:
    """6位代码 + 交易所 -> tushare ts_code，如 ('000001','sz') -> '000001.SZ'。"""
    suffix = {"sh": "SH", "sz": "SZ", "bj": "BJ"}.get(exchange, exchange.upper())
    return f"{symbol}.{suffix}"


def from_ts_code(ts_code: str) -> str:
    """ts_code -> 6位代码，如 '000001.SZ' -> '000001'。"""
    return ts_code.split(".")[0]


def get_universe_ts_codes(conn, symbols: list[str] | None = None, include_delisted: bool = False) -> list[tuple[str, str]]:
    """返回 [(symbol, ts_code), ...]。include_delisted=True 时把已退市股票也纳入
    （用于补齐退市股历史行情/复权因子，是本项目缓解幸存者偏差的关键数据源）。"""
    where = "WHERE exchange IN ('sh','sz','bj')"
    if not include_delisted:
        where += " AND is_delisted = FALSE"
    params: list = []
    if symbols:
        placeholders = ",".join(["?"] * len(symbols))
        where += f" AND symbol IN ({placeholders})"
        params = list(symbols)
    rows = conn.execute(f"SELECT symbol, exchange FROM universe {where}", params).fetchall()
    return [(sym, to_ts_code(sym, exch)) for sym, exch in rows]


def get_trade_dates(conn, start_date: str, end_date: str) -> list[str]:
    """从已同步的 trade_calendar 取交易日列表（YYYYMMDD字符串），供逐交易日批处理使用。
    若 trade_calendar 表为空（尚未跑过 market_data 同步），退化为按自然日周一到周五近似。"""
    rows = conn.execute(
        "SELECT trade_date FROM trade_calendar WHERE trade_date BETWEEN ? AND ? ORDER BY trade_date",
        [pd.to_datetime(start_date).date(), pd.to_datetime(end_date).date()],
    ).fetchall()
    if rows:
        return [d.strftime("%Y%m%d") for (d,) in rows]
    logger.warning("trade_calendar 为空，退化为按自然日工作日近似（建议先跑 ingestion.market_data）")
    bdays = pd.bdate_range(start_date, end_date)
    return [d.strftime("%Y%m%d") for d in bdays]


def upsert(conn, table: str, key_cols: list[str], df: pd.DataFrame) -> int:
    if df.empty:
        return 0
    df = df.drop_duplicates(subset=key_cols, keep="last")
    # DuckDB 主键列隐含 NOT NULL；源数据里买方席位等字段可能为空
    for col in key_cols:
        if col in df.columns and df[col].dtype == object:
            df[col] = df[col].fillna("")
    conn.register("incoming_ts", df)
    key_tuple = ", ".join(key_cols)
    conn.execute(f"DELETE FROM {table} WHERE ({key_tuple}) IN (SELECT {key_tuple} FROM incoming_ts)")
    cols = ", ".join(df.columns)
    conn.execute(f"INSERT INTO {table} ({cols}) SELECT {cols} FROM incoming_ts")
    conn.unregister("incoming_ts")
    return len(df)


def run_symbol_loop_sync(
    task_name: str,
    table: str,
    key_cols: list[str],
    fetch_fn: Callable[[str], pd.DataFrame],
    normalize_fn: Callable[[pd.DataFrame, str], pd.DataFrame],
    targets: list[tuple[str, str]],
    limit: int | None = None,
) -> dict:
    """逐股票批处理通用编排：targets 是 [(symbol, ts_code), ...]。
    fetch_fn(ts_code) -> 原始df；normalize_fn(原始df, symbol) -> 待写入df。"""
    checkpoint_size = get_config()["ingestion"].get("checkpoint_batch_size", 50)
    if limit:
        targets = targets[:limit]

    conn = get_connection()
    init_schema(conn)
    stats = {"ok": 0, "empty": 0, "failed": 0, "rows_written": 0, "failed_symbols": []}

    for i, (symbol, ts_code) in enumerate(tqdm(targets, desc=task_name)):
        try:
            raw = retry_on_failure()(fetch_fn)(ts_code)
            norm = normalize_fn(raw, symbol)
            if norm.empty:
                stats["empty"] += 1
            else:
                stats["rows_written"] += upsert(conn, table, key_cols, norm)
                stats["ok"] += 1
            conn.execute(
                "INSERT INTO sync_log (task_name, symbol, status, message) VALUES (?, ?, ?, ?)",
                [task_name, symbol, "success", f"rows={len(norm)}"],
            )
        except Exception as exc:  # noqa: BLE001 - 单日/单票数据质量问题不得拖垮整批
            stats["failed"] += 1
            stats["failed_symbols"].append(symbol)
            conn.execute(
                "INSERT INTO sync_log (task_name, symbol, status, message) VALUES (?, ?, ?, ?)",
                [task_name, symbol, "failed", f"{type(exc).__name__}: {str(exc)[:280]}"],
            )
            logger.warning("%s: %s 失败: %s: %s", task_name, symbol, type(exc).__name__, exc)

        if (i + 1) % checkpoint_size == 0:
            logger.info("%s 进度 %d/%d: %s", task_name, i + 1, len(targets), stats)

        polite_sleep()

    conn.close()
    logger.info("%s 完成: %s", task_name, stats)
    return stats


def run_date_loop_sync(
    task_name: str,
    table: str,
    key_cols: list[str],
    fetch_fn: Callable[[str], pd.DataFrame],
    normalize_fn: Callable[[pd.DataFrame], pd.DataFrame],
    trade_dates: list[str],
    limit: int | None = None,
) -> dict:
    """逐交易日批处理通用编排：每个交易日一次调用覆盖全市场快照，比逐股票循环快得多。
    fetch_fn(trade_date) -> 原始df；normalize_fn(原始df) -> 待写入df。"""
    checkpoint_size = get_config()["ingestion"].get("checkpoint_batch_size", 50)
    if limit:
        trade_dates = trade_dates[:limit]

    conn = get_connection()
    init_schema(conn)
    stats = {"ok": 0, "empty": 0, "failed": 0, "rows_written": 0, "failed_dates": []}

    for i, trade_date in enumerate(tqdm(trade_dates, desc=task_name)):
        try:
            raw = retry_on_failure()(fetch_fn)(trade_date)
            norm = normalize_fn(raw)
            if norm.empty:
                stats["empty"] += 1
            else:
                stats["rows_written"] += upsert(conn, table, key_cols, norm)
                stats["ok"] += 1
        except Exception as exc:  # noqa: BLE001
            stats["failed"] += 1
            stats["failed_dates"].append(trade_date)
            logger.warning("%s: %s 失败: %s: %s", task_name, trade_date, type(exc).__name__, exc)

        if (i + 1) % checkpoint_size == 0:
            logger.info("%s 进度 %d/%d: %s", task_name, i + 1, len(trade_dates), stats)

        polite_sleep()

    conn.execute(
        "INSERT INTO sync_log (task_name, status, message) VALUES (?, ?, ?)",
        [task_name, "success", str(stats)[:500]],
    )
    conn.close()
    logger.info("%s 完成: %s", task_name, stats)
    return stats
