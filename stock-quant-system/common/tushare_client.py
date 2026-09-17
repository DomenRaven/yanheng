"""
Tushare Pro 共用客户端与通用批处理辅助函数。

背景：2026-08-25 用户升级到 Tushare Pro 2000积分档位（200元/年），解锁了本项目此前
两轮评估文档记录的多项已知缺口所需接口（adj_factor/daily_basic/index_weight/suspend_d
/stk_limit）以及 Phase 2 行为金融层数据源（top_list/moneyflow/margin/pledge_stat/
stk_holdernumber/block_trade）。2000档限频 200次/分钟、100000次/天/接口，足够覆盖
全市场~5500只股票或~10年交易日的批量抓取需求。

token 存放在项目根目录 .env（TUSHARE_TOKEN=...），不硬编码进代码，.env 已在 .gitignore。

两类批处理模式：
    - "逐股票"：每只股票一次调用返回其全部历史（如 adj_factor），适用于按 ts_code 长历史接口。
    - "逐交易日"：每个交易日一次调用返回全市场当日快照（如 daily_basic/suspend_d/stk_limit/
      top_list），比逐股票循环 API 次数更少，是全量灌库首选。
"""
from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Callable

import pandas as pd
import tushare as ts

from common.config import get_config
from common.db import write_session
from common.http_retry import retry_on_failure, tushare_limiter
from common.parallel_fetch import map_fetch_then_write

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
    """逐股票批处理。HTTP 在循环里，写入走 write_session（短连接 + 进程内写锁）。
    Tushare 客户端非线程安全，workers 固定 1。"""
    checkpoint_size = get_config()["ingestion"].get("checkpoint_batch_size", 50)
    if limit:
        targets = targets[:limit]

    with write_session(init=True):
        pass

    stats = {"ok": 0, "empty": 0, "failed": 0, "rows_written": 0, "failed_symbols": []}
    done = {"n": 0}

    def fetch(item: tuple[str, str]) -> pd.DataFrame:
        _symbol, ts_code = item
        return retry_on_failure()(fetch_fn)(ts_code)

    def on_result(item: tuple[str, str], raw: pd.DataFrame | None, err: BaseException | None) -> None:
        symbol, _ts = item
        done["n"] += 1
        if err is not None:
            stats["failed"] += 1
            stats["failed_symbols"].append(symbol)
            with write_session() as conn:
                conn.execute(
                    "INSERT INTO sync_log (task_name, symbol, status, message) VALUES (?, ?, ?, ?)",
                    [task_name, symbol, "failed", f"{type(err).__name__}: {str(err)[:280]}"],
                )
            logger.warning("%s: %s 失败: %s: %s", task_name, symbol, type(err).__name__, err)
            return
        norm = normalize_fn(raw, symbol)
        with write_session() as conn:
            if norm.empty:
                stats["empty"] += 1
            else:
                stats["rows_written"] += upsert(conn, table, key_cols, norm)
                stats["ok"] += 1
            conn.execute(
                "INSERT INTO sync_log (task_name, symbol, status, message) VALUES (?, ?, ?, ?)",
                [task_name, symbol, "success", f"rows={len(norm)}"],
            )
        if done["n"] % checkpoint_size == 0:
            logger.info("%s 进度 %d/%d: %s", task_name, done["n"], len(targets), stats)

    map_fetch_then_write(
        targets, fetch, on_result, workers=1, desc=task_name, limiter=tushare_limiter()
    )
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
    """逐交易日批处理。HTTP 与写入拆开，避免整段占着 DuckDB。"""
    checkpoint_size = get_config()["ingestion"].get("checkpoint_batch_size", 50)
    if limit:
        trade_dates = trade_dates[:limit]

    with write_session(init=True):
        pass

    stats = {"ok": 0, "empty": 0, "failed": 0, "rows_written": 0, "failed_dates": []}
    done = {"n": 0}

    def fetch(trade_date: str) -> pd.DataFrame:
        return retry_on_failure()(fetch_fn)(trade_date)

    def on_result(trade_date: str, raw: pd.DataFrame | None, err: BaseException | None) -> None:
        done["n"] += 1
        if err is not None:
            stats["failed"] += 1
            stats["failed_dates"].append(trade_date)
            logger.warning("%s: %s 失败: %s: %s", task_name, trade_date, type(err).__name__, err)
            return
        norm = normalize_fn(raw)
        with write_session() as conn:
            if norm.empty:
                stats["empty"] += 1
            else:
                stats["rows_written"] += upsert(conn, table, key_cols, norm)
                stats["ok"] += 1
        if done["n"] % checkpoint_size == 0:
            logger.info("%s 进度 %d/%d: %s", task_name, done["n"], len(trade_dates), stats)

    map_fetch_then_write(
        trade_dates, fetch, on_result, workers=1, desc=task_name, limiter=tushare_limiter()
    )
    with write_session() as conn:
        conn.execute(
            "INSERT INTO sync_log (task_name, status, message) VALUES (?, ?, ?)",
            [task_name, "success", str(stats)[:500]],
        )
    logger.info("%s 完成: %s", task_name, stats)
    return stats


def run_item_loop_sync(
    task_name: str,
    table: str,
    key_cols: list[str],
    items: list,
    fetch_fn: Callable,
    normalize_fn: Callable[[pd.DataFrame, object], pd.DataFrame],
    limit: int | None = None,
    *,
    item_label: Callable[[object], str] | None = None,
) -> dict:
    """通用逐条批处理（如 index_code+trade_date 二元组）。fetch 与 write 分离。"""
    checkpoint_size = get_config()["ingestion"].get("checkpoint_batch_size", 50)
    if limit:
        items = items[:limit]
    label = item_label or (lambda x: str(x))

    with write_session(init=True):
        pass

    stats = {"ok": 0, "empty": 0, "failed": 0, "rows_written": 0, "failed_items": []}
    done = {"n": 0}

    def fetch(item: object) -> pd.DataFrame:
        return retry_on_failure()(fetch_fn)(item)

    def on_result(item: object, raw: pd.DataFrame | None, err: BaseException | None) -> None:
        done["n"] += 1
        if err is not None:
            stats["failed"] += 1
            stats["failed_items"].append(label(item))
            logger.warning("%s: %s 失败: %s: %s", task_name, label(item), type(err).__name__, err)
            return
        norm = normalize_fn(raw, item)
        with write_session() as conn:
            if norm.empty:
                stats["empty"] += 1
            else:
                stats["rows_written"] += upsert(conn, table, key_cols, norm)
                stats["ok"] += 1
        if done["n"] % checkpoint_size == 0:
            logger.info("%s 进度 %d/%d: %s", task_name, done["n"], len(items), stats)

    map_fetch_then_write(
        items, fetch, on_result, workers=1, desc=task_name, limiter=tushare_limiter()
    )
    with write_session() as conn:
        conn.execute(
            "INSERT INTO sync_log (task_name, status, message) VALUES (?, ?, ?)",
            [task_name, "success", str(stats)[:500]],
        )
    logger.info("%s 完成: %s", task_name, stats)
    return stats
