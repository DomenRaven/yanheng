"""掘金扫描存档与坚持度榜（需求 R1）。

去重键：(pool_id, asof_trade_date, model_run_id)。
「前 50 坚持度」是截面排名稳定性观察，不是荐股/胜率。
"""
from __future__ import annotations

import datetime as dt
import logging
from typing import Any

import pandas as pd

logger = logging.getLogger("advice.scan_archive")

DEFAULT_POOL = "all"
TOP_K_DEFAULT = 50


def scan_run_exists(conn, pool_id: str, asof: dt.date, model_run_id: str) -> bool:
    row = conn.execute(
        """
        SELECT 1 FROM scan_run
        WHERE pool_id = ? AND asof_trade_date = ? AND model_run_id = ?
        LIMIT 1
        """,
        [pool_id, asof, model_run_id],
    ).fetchone()
    return row is not None


def record_scan_archive(
    conn,
    *,
    ranked: pd.DataFrame,
    trade_date: dt.date | str,
    model_run_id: str,
    pool_id: str = DEFAULT_POOL,
    source: str = "ui",
) -> dict[str, Any]:
    """写入/更新 prediction_log + scan_run。同键重复扫描：upsert 分数，不新增平行 run。"""
    from common.tushare_client import upsert

    asof = pd.Timestamp(trade_date).date()
    existed = scan_run_exists(conn, pool_id, asof, model_run_id)

    log_df = ranked[["symbol", "rank", "pred_score", "is_tradable"]].copy()
    log_df["trade_date"] = asof
    log_df["model_run_id"] = model_run_id
    log_df["pool_id"] = pool_id
    n = upsert(
        conn,
        "prediction_log",
        ["symbol", "trade_date", "model_run_id"],
        log_df[["symbol", "trade_date", "model_run_id", "pred_score", "rank", "is_tradable", "pool_id"]],
    )

    conn.execute(
        """
        DELETE FROM scan_run
        WHERE pool_id = ? AND asof_trade_date = ? AND model_run_id = ?
        """,
        [pool_id, asof, model_run_id],
    )
    conn.execute(
        """
        INSERT INTO scan_run (pool_id, asof_trade_date, model_run_id, universe_size, source, scanned_at)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        [pool_id, asof, model_run_id, int(len(ranked)), source, dt.datetime.now()],
    )
    logger.info(
        "scan_archive pool=%s asof=%s model=%s rows=%s existed=%s",
        pool_id,
        asof,
        model_run_id,
        n,
        existed,
    )
    return {"existed": existed, "rows_upserted": n, "pool_id": pool_id, "asof": asof}


def list_recent_scan_dates(conn, pool_id: str = DEFAULT_POOL, limit: int = 20) -> list[dt.date]:
    rows = conn.execute(
        """
        SELECT asof_trade_date FROM scan_run
        WHERE pool_id = ?
        ORDER BY asof_trade_date DESC
        LIMIT ?
        """,
        [pool_id, limit],
    ).fetchall()
    if not rows:
        # 兼容旧库：尚无 scan_run 时用 prediction_log 截面日
        rows = conn.execute(
            """
            SELECT DISTINCT trade_date FROM prediction_log
            WHERE coalesce(pool_id, 'all') = ?
            ORDER BY trade_date DESC
            LIMIT ?
            """,
            [pool_id, limit],
        ).fetchall()
        if not rows and pool_id == DEFAULT_POOL:
            rows = conn.execute(
                """
                SELECT DISTINCT trade_date FROM prediction_log
                ORDER BY trade_date DESC
                LIMIT ?
                """,
                [limit],
            ).fetchall()
    return [r[0] if isinstance(r[0], dt.date) else pd.Timestamp(r[0]).date() for r in rows]


def persistence_full_attendance(
    conn,
    *,
    pool_id: str = DEFAULT_POOL,
    window_runs: int = 5,
    top_k: int = TOP_K_DEFAULT,
) -> pd.DataFrame:
    """最近 window_runs 次有效扫描中，每次都进入前 top_k 的股票（全勤）。"""
    dates = list_recent_scan_dates(conn, pool_id, limit=window_runs)
    if len(dates) < window_runs:
        return pd.DataFrame(
            columns=["symbol", "name", "hits", "window", "avg_rank", "best_rank"]
        )
    placeholders = ",".join(["?"] * len(dates))
    df = conn.execute(
        f"""
        SELECT p.symbol, u.name,
               count(DISTINCT p.trade_date) AS hits,
               avg(p.rank) AS avg_rank,
               min(p.rank) AS best_rank
        FROM prediction_log p
        LEFT JOIN universe u ON u.symbol = p.symbol
        WHERE coalesce(p.pool_id, 'all') = ?
          AND p.trade_date IN ({placeholders})
          AND p.rank <= ?
        GROUP BY p.symbol, u.name
        HAVING count(DISTINCT p.trade_date) = ?
        ORDER BY avg_rank ASC, p.symbol
        """,
        [pool_id, *dates, top_k, len(dates)],
    ).df()
    if not df.empty:
        df["window"] = len(dates)
    return df


def persistence_hit_counts(
    conn,
    *,
    pool_id: str = DEFAULT_POOL,
    lookback_runs: int = 20,
    top_k: int = TOP_K_DEFAULT,
) -> pd.DataFrame:
    """最近 lookback_runs 次扫描中，进入前 top_k 的次数。"""
    dates = list_recent_scan_dates(conn, pool_id, limit=lookback_runs)
    if not dates:
        return pd.DataFrame(columns=["symbol", "name", "hits", "window", "hit_ratio", "avg_rank"])
    placeholders = ",".join(["?"] * len(dates))
    df = conn.execute(
        f"""
        SELECT p.symbol, u.name,
               count(DISTINCT p.trade_date) AS hits,
               avg(p.rank) AS avg_rank,
               min(p.rank) AS best_rank
        FROM prediction_log p
        LEFT JOIN universe u ON u.symbol = p.symbol
        WHERE coalesce(p.pool_id, 'all') = ?
          AND p.trade_date IN ({placeholders})
          AND p.rank <= ?
        GROUP BY p.symbol, u.name
        ORDER BY hits DESC, avg_rank ASC
        """,
        [pool_id, *dates, top_k],
    ).df()
    if not df.empty:
        df["window"] = len(dates)
        df["hit_ratio"] = df["hits"] / float(len(dates))
    return df


def symbol_rank_history(
    conn,
    symbol: str,
    *,
    pool_id: str | None = None,
    limit: int = 60,
) -> pd.DataFrame:
    if pool_id:
        return conn.execute(
            """
            SELECT trade_date, rank, pred_score, model_run_id, coalesce(pool_id, 'all') AS pool_id
            FROM prediction_log
            WHERE symbol = ? AND coalesce(pool_id, 'all') = ?
            ORDER BY trade_date DESC
            LIMIT ?
            """,
            [symbol, pool_id, limit],
        ).df()
    return conn.execute(
        """
        SELECT trade_date, rank, pred_score, model_run_id, coalesce(pool_id, 'all') AS pool_id
        FROM prediction_log
        WHERE symbol = ?
        ORDER BY trade_date DESC
        LIMIT ?
        """,
        [symbol, limit],
    ).df()
