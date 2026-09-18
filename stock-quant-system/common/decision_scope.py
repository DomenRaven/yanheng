"""日更决策范围：持仓 + 模拟持仓 + 近几日建议/待办标的。

对照 requirements-20260918 R3：工作日不扫全市场日 K，只刷新「要用的那几只」。
"""
from __future__ import annotations

import datetime as dt
import logging

logger = logging.getLogger("common.decision_scope")


def decision_quote_symbols(conn, *, lookback_days: int = 7) -> list[str]:
    """返回去重后的股票代码列表（可能为空）。"""
    syms: set[str] = set()
    try:
        for (s,) in conn.execute(
            "SELECT DISTINCT symbol FROM positions WHERE COALESCE(is_closed, FALSE) = FALSE"
        ).fetchall():
            if s:
                syms.add(str(s))
    except Exception as exc:  # noqa: BLE001
        logger.warning("读 positions 失败: %s", exc)

    try:
        for (s,) in conn.execute("SELECT DISTINCT symbol FROM paper_positions").fetchall():
            if s:
                syms.add(str(s))
    except Exception as exc:  # noqa: BLE001
        logger.warning("读 paper_positions 失败: %s", exc)

    since = dt.date.today() - dt.timedelta(days=lookback_days)
    try:
        for (s,) in conn.execute(
            """
            SELECT DISTINCT symbol FROM advice_log
            WHERE as_of >= ?
              AND action IN ('open', 'reduce', 'stop_loss', 'take_profit', 'rebalance')
            """,
            [since],
        ).fetchall():
            if s:
                syms.add(str(s))
    except Exception as exc:  # noqa: BLE001
        logger.warning("读 advice_log 失败: %s", exc)

    return sorted(syms)


def split_hs_bj(conn, symbols: list[str]) -> tuple[list[str], list[str]]:
    """按 universe.exchange 拆成沪深与北交所；未知代码归沪深路径尝试。"""
    if not symbols:
        return [], []
    placeholders = ",".join(["?"] * len(symbols))
    rows = conn.execute(
        f"SELECT symbol, exchange FROM universe WHERE symbol IN ({placeholders})",
        list(symbols),
    ).fetchall()
    exch = {s: e for s, e in rows}
    hs, bj = [], []
    for s in symbols:
        if exch.get(s) == "bj":
            bj.append(s)
        else:
            hs.append(s)
    return hs, bj
