"""建议 → 成交全链路追溯（规格 M16 / S7 / S9 共用查询）。

理论：
- `docs/07-产品设计启示/02-场景化建议引擎.md`：建议必须可审计、可回放。
- `docs/04-风险管理/11-纸面账户与影子交易.md` §2：Perold Implementation Shortfall
  的日频对应物是「决策日收盘纸面价」vs「次日成交路径」，未成交必须保留拒绝原因、
  禁止用收盘价补假成交。
- `docs/03-量化方法/15-日频信号与次日执行.md`：fill_vs_close 是相对「信号日收盘
  立刻买」的成本差，不是择时 alpha。
- `docs/04-风险管理/02-仓位与资金管理.md` §5：计划仓位 vs 实际仓位（S9）。

页面禁止复制本文件里的 JOIN / 缺口公式。
"""
from __future__ import annotations

from typing import Any

import pandas as pd

from common.config import get_config


def _price_adjust() -> str:
    pt = get_config().get("paper_trading") or {}
    return str(pt.get("price_adjust", "qfq"))


def load_advice_traces(conn, *, advice_id: str | None = None) -> pd.DataFrame:
    """LEFT JOIN advice_log 到成功成交（reject_reason IS NULL）。

    同一 advice_id 若有多笔成功成交（本不应发生，M19 幂等），取 trade_date 最晚一笔。
    """
    adj = _price_adjust()
    sql = """
        WITH fills AS (
            SELECT
                trade_id, advice_id, symbol, side, shares, price, fees,
                trade_date, source,
                ROW_NUMBER() OVER (
                    PARTITION BY advice_id
                    ORDER BY trade_date DESC, trade_id DESC
                ) AS rn
            FROM paper_trades
            WHERE reject_reason IS NULL
              AND advice_id IS NOT NULL
        )
        SELECT
            a.advice_id,
            a.as_of,
            a.symbol,
            a.name,
            a.action,
            a.size_shares AS planned_shares,
            a.est_amount_cny AS planned_amount_cny,
            a.size_pct_nav,
            a.max_loss_cny,
            a.exec_date,
            a.plain_summary,
            a.confidence,
            a.model_run_id,
            f.trade_id AS fill_trade_id,
            f.side AS fill_side,
            f.shares AS fill_shares,
            f.price AS fill_price,
            f.fees AS fill_fees,
            f.trade_date AS fill_date,
            f.source AS fill_source,
            dq.close AS advice_close
        FROM advice_log a
        LEFT JOIN fills f
          ON f.advice_id = a.advice_id AND f.rn = 1
        LEFT JOIN daily_quotes dq
          ON dq.symbol = a.symbol
         AND dq.trade_date = a.as_of
         AND dq.adjust = ?
    """
    params: list[Any] = [adj]
    if advice_id:
        sql += " WHERE a.advice_id = ?"
        params.append(advice_id)
    sql += " ORDER BY a.as_of DESC, a.created_at DESC"
    df = conn.execute(sql, params).df()
    return enrich_trace_frame(df)


def enrich_trace_frame(df: pd.DataFrame) -> pd.DataFrame:
    """在查询结果上计算完整度、股数缺口、相对建议日收盘的价差。"""
    if df.empty:
        df["trace_complete"] = pd.Series(dtype=bool)
        df["shares_gap"] = pd.Series(dtype=float)
        df["fill_vs_advice_close"] = pd.Series(dtype=float)
        return df
    has_fill = df["fill_trade_id"].notna()
    df = df.copy()
    df["trace_complete"] = has_fill
    planned = pd.to_numeric(df["planned_shares"], errors="coerce")
    filled = pd.to_numeric(df["fill_shares"], errors="coerce")
    df["shares_gap"] = filled - planned
    close = pd.to_numeric(df["advice_close"], errors="coerce")
    px = pd.to_numeric(df["fill_price"], errors="coerce")
    df["fill_vs_advice_close"] = (px / close) - 1.0
    df.loc[close.isna() | (close <= 0) | px.isna(), "fill_vs_advice_close"] = pd.NA
    return df


def complete_trace_count(traces: pd.DataFrame) -> int:
    if traces.empty or "trace_complete" not in traces.columns:
        return 0
    return int(traces["trace_complete"].fillna(False).sum())
