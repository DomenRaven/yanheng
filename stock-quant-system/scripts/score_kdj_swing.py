"""用仓库日线给「高低点 + KDJ」规则打分。

只读 DuckDB，不写行情库，不改冠军模型。
协议见 research.kdj_swing.PROTOCOL。结果写 docs/kdj-swing-score-20260922.json，
逐笔写 data/kdj_swing_trades_20260922.parquet（gitignore）。
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT))

from common.db import get_connection
from research.kdj_swing import (
    PROTOCOL,
    BookAccum,
    attach_swing_signals,
    compute_kdj_frame,
    simulate_symbol_frame,
    summarize_book,
    summarize_trades,
)

CHUNK = 250
OUT_JSON = _ROOT / "docs" / "kdj-swing-score-20260922.json"
OUT_PARQUET = _ROOT / "data" / "kdj_swing_trades_20260922.parquet"


def _query(conn, symbols: list[str]) -> pd.DataFrame:
    marks = ",".join(["?"] * len(symbols))
    sql = f"""
        WITH last_adj AS (
            SELECT symbol, arg_max(adj_factor, trade_date) AS adj_last
            FROM adj_factor
            WHERE symbol IN ({marks})
            GROUP BY symbol
        )
        SELECT
            q.symbol,
            q.trade_date,
            q.open, q.high, q.low, q.close,
            u.exchange,
            u.board,
            COALESCE(u.is_st, FALSE) AS is_st,
            lp.up_limit,
            lp.down_limit,
            (sc.trade_date IS NOT NULL) AS suspended,
            CASE
                WHEN af.adj_factor IS NULL OR af.adj_factor = 0 OR la.adj_last IS NULL THEN NULL
                ELSE q.open * la.adj_last / af.adj_factor
            END AS raw_open
        FROM daily_quotes q
        LEFT JOIN universe u ON u.symbol = q.symbol
        LEFT JOIN limit_price lp
          ON lp.symbol = q.symbol AND lp.trade_date = q.trade_date
        LEFT JOIN adj_factor af
          ON af.symbol = q.symbol AND af.trade_date = q.trade_date
        LEFT JOIN last_adj la ON la.symbol = q.symbol
        LEFT JOIN suspend_calendar sc
          ON sc.symbol = q.symbol AND sc.trade_date = q.trade_date AND sc.suspend_type = 'S'
        WHERE q.adjust = 'qfq'
          AND q.symbol IN ({marks})
          AND q.open > 0 AND q.high > 0 AND q.low > 0 AND q.close > 0
          AND q.high >= q.low
        ORDER BY q.symbol, q.trade_date
    """
    return conn.execute(sql, symbols + symbols).df()


def _attach_index(trades: pd.DataFrame, index: pd.DataFrame) -> pd.DataFrame:
    if trades.empty:
        return trades
    out = trades.copy()
    opens = index["open"]
    closes = index["close"]
    entry = pd.to_datetime(out["entry_date"]).dt.date
    exit_ = pd.to_datetime(out["exit_date"]).dt.date
    entry_open = entry.map(opens)
    exit_px = np.where(
        out["exit_reason"].to_numpy() != "signal",
        exit_.map(closes).to_numpy(dtype=float),
        exit_.map(opens).to_numpy(dtype=float),
    )
    entry_arr = entry_open.to_numpy(dtype=float)
    with np.errstate(divide="ignore", invalid="ignore"):
        idx_ret = exit_px / entry_arr - 1.0
    idx_ret = np.where(np.isfinite(entry_arr) & (entry_arr > 0) & np.isfinite(exit_px), idx_ret, np.nan)
    out["index_return"] = idx_ret
    out["excess_gross"] = out["gross_return"].to_numpy(dtype=float) - idx_ret
    out["excess_net"] = out["net_return"].to_numpy(dtype=float) - idx_ret
    return out


def _jsonable(obj):
    if isinstance(obj, dict):
        return {str(k): _jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_jsonable(v) for v in obj]
    if isinstance(obj, (np.floating, float)):
        if not np.isfinite(obj):
            return None
        return float(obj)
    if isinstance(obj, (np.integer,)):
        return int(obj)
    if isinstance(obj, (np.bool_, bool)):
        return bool(obj)
    return obj


def main() -> None:
    t0 = time.perf_counter()
    conn = get_connection(read_only=True)
    try:
        symbols = conn.execute(
            """
            SELECT symbol FROM daily_quotes
            WHERE adjust = 'qfq'
            GROUP BY symbol
            ORDER BY symbol
            """
        ).df()["symbol"].tolist()
        index = conn.execute(
            """
            SELECT trade_date, open, close
            FROM index_quotes
            WHERE index_code = 'sh000300'
            ORDER BY trade_date
            """
        ).df()
        index["trade_date"] = pd.to_datetime(index["trade_date"]).dt.date
        index = index.drop_duplicates("trade_date").set_index("trade_date")

        trades: list[dict] = []
        counts = {
            "n_buy_signal": 0,
            "n_buy_ignored_in_position": 0,
            "n_entry_unfilled": 0,
            "n_entry_missing_limit": 0,
            "n_signal_expired_eod": 0,
        }
        book = BookAccum()
        n_rows = 0
        dmin = None
        dmax = None
        n_chunks = (len(symbols) + CHUNK - 1) // CHUNK
        for i in range(0, len(symbols), CHUNK):
            chunk = symbols[i : i + CHUNK]
            step = i // CHUNK + 1
            t_chunk = time.perf_counter()
            raw = _query(conn, chunk)
            n_rows += len(raw)
            if not raw.empty:
                d0 = pd.to_datetime(raw["trade_date"]).min()
                d1 = pd.to_datetime(raw["trade_date"]).max()
                dmin = d0 if dmin is None else min(dmin, d0)
                dmax = d1 if dmax is None else max(dmax, d1)
                priced = compute_kdj_frame(raw)
                signaled = attach_swing_signals(priced)
                for _, g in signaled.groupby("symbol", sort=False):
                    got, part = simulate_symbol_frame(g, book=book)
                    trades.extend(got)
                    for key in counts:
                        counts[key] += part[key]
            elapsed = time.perf_counter() - t_chunk
            print(
                f"chunk {step}/{n_chunks} symbols={len(chunk)} rows={len(raw)} "
                f"trades={len(trades)} {elapsed:.1f}s",
                flush=True,
            )
    finally:
        conn.close()

    frame = pd.DataFrame(trades)
    frame = _attach_index(frame, index)
    if not frame.empty:
        OUT_PARQUET.parent.mkdir(parents=True, exist_ok=True)
        frame.to_parquet(OUT_PARQUET, index=False)
    summary = summarize_trades(frame)
    book_stats = summarize_book(book, index["close"])
    filled = int(len(frame))
    attempts = counts["n_buy_signal"] - counts["n_buy_ignored_in_position"]
    payload = {
        "protocol": PROTOCOL,
        "coverage": {
            "n_symbols": len(symbols),
            "n_rows": n_rows,
            "date_min": None if dmin is None else str(pd.Timestamp(dmin).date()),
            "date_max": None if dmax is None else str(pd.Timestamp(dmax).date()),
            "adjust": "qfq",
            "benchmark": "sh000300",
        },
        "signals": {
            **counts,
            "n_entries": filled,
            "entry_fill_rate": round(filled / attempts, 4) if attempts else None,
        },
        "score": summary,
        "book": book_stats,
        "seconds": round(time.perf_counter() - t0, 1),
    }
    OUT_JSON.write_text(json.dumps(_jsonable(payload), ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"wrote {OUT_JSON}", flush=True)
    print(f"wrote {OUT_PARQUET}", flush=True)


if __name__ == "__main__":
    main()
