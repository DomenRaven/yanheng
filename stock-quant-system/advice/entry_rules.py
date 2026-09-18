"""次日执行与明日待办（Phase 5 阶段 C，纯函数 + 轻量查库）。"""
from __future__ import annotations

import datetime as dt

import pandas as pd

from common.tushare_client import get_trade_dates


def next_trade_date(conn, signal_date: dt.date) -> dt.date | None:
    start = signal_date.strftime("%Y%m%d")
    end = (signal_date + dt.timedelta(days=30)).strftime("%Y%m%d")
    dates = get_trade_dates(conn, start, end)
    sig = signal_date.strftime("%Y%m%d")
    for d in dates:
        if d > sig:
            return pd.to_datetime(d, format="%Y%m%d").date()
    return None


def entry_blocked_at_open(conn, symbol: str, exec_date: dt.date, *, adjust: str = "qfq") -> tuple[bool, str]:
    """True = 不可按开盘价买入（停牌或开盘封涨停）。"""
    row = conn.execute(
        """
        SELECT lp.up_limit, dq.open,
               (sc.trade_date IS NOT NULL) AS suspended
        FROM limit_price lp
        LEFT JOIN daily_quotes dq
          ON dq.symbol = lp.symbol AND dq.trade_date = lp.trade_date AND dq.adjust = ?
        LEFT JOIN suspend_calendar sc
          ON sc.symbol = lp.symbol AND sc.trade_date = lp.trade_date AND sc.suspend_type = 'S'
        WHERE lp.symbol = ? AND lp.trade_date = ?
        """,
        [adjust, symbol, exec_date],
    ).fetchone()
    if row is None:
        return False, ""
    up, open_px, suspended = row
    if suspended:
        return True, "执行日停牌"
    if open_px is None or up is None:
        return False, ""
    if float(open_px) >= float(up) * 0.998:
        return True, "执行日开盘触及涨停"
    return False, ""


def build_tomorrow_todos(
    cards: list[dict],
    signal_date: dt.date,
    conn,
    *,
    max_items: int = 3,
    allow_bj_open: bool | None = None,
) -> list[dict]:
    """从 open/reduce/stop_loss 等卡片挑最多 max_items 条明日待办。

    allow_bj_open=False（默认，读 config）时：不把北交所 open 放进待办（持仓卖/减仍保留）。
    """
    from common.config import get_config

    if allow_bj_open is None:
        allow_bj_open = bool((get_config().get("paper_trading") or {}).get("allow_bj_open_in_todos", False))

    exec_d = next_trade_date(conn, signal_date)
    if exec_d is None:
        return []

    def _is_bj(symbol: str) -> bool:
        row = conn.execute("SELECT exchange FROM universe WHERE symbol = ?", [symbol]).fetchone()
        return bool(row and row[0] == "bj")

    priority = {"stop_loss": 0, "reduce": 1, "take_profit": 2, "open": 3, "rebalance": 4}
    actionable = []
    for c in cards:
        act = c.get("action")
        if act not in priority:
            continue
        if act == "open" and c.get("size_shares", 0) < 100:
            continue
        if act == "open" and not allow_bj_open and _is_bj(str(c["symbol"])):
            continue
        blocked, why = (False, "")
        if act == "open":
            blocked, why = entry_blocked_at_open(conn, c["symbol"], exec_d)
        item = {
            "advice_id": c.get("advice_id"),
            "symbol": c["symbol"],
            "name": c.get("name"),
            "action": act,
            "signal_date": signal_date.isoformat(),
            "exec_date": exec_d.isoformat(),
            "size_shares": c.get("size_shares"),
            "est_amount_cny": c.get("est_amount_cny"),
            "blocked": blocked,
            "block_reason": why if blocked else None,
            "plain_summary": c.get("plain_summary"),
        }
        actionable.append((priority.get(act, 9), -c.get("confidence", 0), item))

    actionable.sort(key=lambda x: (x[0], x[1]))
    ordered = [x[2] for x in actionable]
    from advice.industry_cap import cap_opens_one_per_industry, latest_industry_codes

    ind_map = latest_industry_codes(conn, [i["symbol"] for i in ordered], signal_date)
    return cap_opens_one_per_industry(ordered, ind_map, max_items=max_items)
