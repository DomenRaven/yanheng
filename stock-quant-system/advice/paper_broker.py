"""本机 A 股模拟成交（Phase 5 阶段 A）。

规则与费用复用 research.a_share_rules；可成交性查 limit_price / suspend_calendar / daily_quotes。
不写入 positions 表。
"""
from __future__ import annotations

import datetime as dt
import logging
import uuid
from dataclasses import dataclass
from typing import Any, Literal

import pandas as pd

from common.config import get_config
from common.db import get_connection, init_schema, write_session
from research.a_share_rules import CostAssumptions, apply_costs

logger = logging.getLogger("advice.paper_broker")

Side = Literal["buy", "sell"]
FillSource = Literal["sim", "user_confirmed_live"]


@dataclass(frozen=True)
class PaperOrderResult:
    status: Literal["filled", "rejected", "skipped", "pending"]
    trade_id: str | None
    reject_reason: str | None
    price: float | None
    fees: float | None
    cash_after: float | None


def _paper_cfg() -> dict[str, Any]:
    return get_config().get("paper_trading") or {}


def _templates() -> dict[str, dict]:
    return _paper_cfg().get("templates") or {}


def _slippage_bp() -> float:
    return float(_paper_cfg().get("slippage_bp", 10))


def _price_adjust() -> str:
    return str(_paper_cfg().get("price_adjust", "qfq"))


def _new_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:12]}"


def create_paper_account(
    template_id: str,
    *,
    note: str | None = None,
    kind: str = "human",
    strategy_id: str | None = None,
    cohort_id: str | None = None,
) -> str:
    templates = _templates()
    if template_id not in templates:
        raise ValueError(f"未知 paper 模板: {template_id}，可选: {list(templates)}")
    kind = kind or "human"
    if kind not in ("human", "shadow"):
        raise ValueError(f"未知 paper kind={kind}")
    initial = float(templates[template_id]["initial_cash_cny"])
    if kind == "human":
        account_id = _new_id(template_id)
    elif cohort_id:
        # 周队列：shadow-2026W38-hs_todos
        slug = str(cohort_id).replace("-", "")
        account_id = f"shadow-{slug}-{strategy_id or _new_id('s')}"
    else:
        account_id = f"shadow-{strategy_id or _new_id('s')}"
    with write_session(init=True) as conn:
        conn.execute(
            """
            INSERT INTO paper_account
              (account_id, template_id, initial_cash, note, kind, strategy_id, cohort_id)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            [account_id, template_id, initial, note, kind, strategy_id, cohort_id],
        )
        conn.execute(
            "INSERT INTO paper_cash (account_id, cash_cny) VALUES (?, ?)",
            [account_id, initial],
        )
    logger.info(
        "创建模拟账户 %s 模板=%s kind=%s strategy=%s cohort=%s 初始现金=%.2f",
        account_id, template_id, kind, strategy_id, cohort_id, initial,
    )
    return account_id


def get_cash(conn, account_id: str) -> float:
    row = conn.execute("SELECT cash_cny FROM paper_cash WHERE account_id = ?", [account_id]).fetchone()
    if row is None:
        raise KeyError(f"paper 账户不存在: {account_id}")
    return float(row[0])


def list_paper_positions(conn, account_id: str) -> pd.DataFrame:
    return conn.execute(
        "SELECT symbol, shares, avg_cost, opened_at FROM paper_positions WHERE account_id = ? ORDER BY symbol",
        [account_id],
    ).df()


def _exchange_for(conn, symbol: str) -> str:
    row = conn.execute("SELECT exchange FROM universe WHERE symbol = ?", [symbol]).fetchone()
    return row[0] if row else "sh"


def _exec_open_price(conn, symbol: str, trade_date: dt.date) -> float | None:
    adj = _price_adjust()
    row = conn.execute(
        """
        SELECT open FROM daily_quotes
        WHERE symbol = ? AND trade_date = ? AND adjust = ?
        """,
        [symbol, trade_date, adj],
    ).fetchone()
    if row is None or row[0] is None:
        return None
    return float(row[0])


def _apply_slippage(side: Side, open_px: float) -> float:
    bp = _slippage_bp() / 10_000.0
    if side == "buy":
        return open_px * (1.0 + bp)
    return open_px * (1.0 - bp)


def _tradability_row(conn, symbol: str, trade_date: dt.date) -> dict[str, Any]:
    adj = _price_adjust()
    row = conn.execute(
        """
        SELECT lp.up_limit, lp.down_limit, dq.open, dq.close,
               (sc.trade_date IS NOT NULL) AS is_suspended
        FROM limit_price lp
        LEFT JOIN daily_quotes dq
          ON dq.symbol = lp.symbol AND dq.trade_date = lp.trade_date AND dq.adjust = ?
        LEFT JOIN suspend_calendar sc
          ON sc.symbol = lp.symbol AND sc.trade_date = lp.trade_date AND sc.suspend_type = 'S'
        WHERE lp.symbol = ? AND lp.trade_date = ?
        """,
        [adj, symbol, trade_date],
    ).fetchone()
    if row is None:
        return {"open": _exec_open_price(conn, symbol, trade_date), "suspended": False, "up": None, "down": None}
    up, down, open_px, close_px, suspended = row
    open_px = open_px if open_px is not None else _exec_open_price(conn, symbol, trade_date)
    return {
        "open": float(open_px) if open_px is not None else None,
        "close": float(close_px) if close_px is not None else None,
        "up": float(up) if up is not None else None,
        "down": float(down) if down is not None else None,
        "suspended": bool(suspended),
    }


def _reject_buy(tr: dict[str, Any], exec_open: float) -> str | None:
    if tr["suspended"]:
        return "停牌不可买入"
    if tr["open"] is None:
        return "缺少开盘价"
    if tr["up"] is not None and exec_open >= tr["up"] * 0.998:
        return "涨停价附近不可买入"
    return None


def _reject_sell(tr: dict[str, Any], exec_open: float) -> str | None:
    if tr["suspended"]:
        return "停牌不可卖出"
    if tr["open"] is None:
        return "缺少开盘价"
    if tr["down"] is not None and exec_open <= tr["down"] * 1.002:
        return "跌停价附近不可卖出"
    return None


def _sellable_shares(conn, account_id: str, symbol: str, trade_date: dt.date) -> float:
    pos = conn.execute(
        "SELECT shares FROM paper_positions WHERE account_id = ? AND symbol = ?",
        [account_id, symbol],
    ).fetchone()
    held = float(pos[0]) if pos else 0.0
    bought_today = conn.execute(
        """
        SELECT COALESCE(SUM(shares), 0) FROM paper_trades
        WHERE account_id = ? AND symbol = ? AND trade_date = ? AND side = 'buy'
          AND reject_reason IS NULL
        """,
        [account_id, symbol, trade_date],
    ).fetchone()[0]
    return max(0.0, held - float(bought_today))


def _advice_already_filled(conn, advice_id: str | None, account_id: str | None = None) -> bool:
    """同一账户内同一 advice_id 只成交一次；不同账户可各自模拟。"""
    if not advice_id:
        return False
    if account_id:
        row = conn.execute(
            """
            SELECT 1 FROM paper_trades
            WHERE advice_id = ? AND account_id = ? AND reject_reason IS NULL
            LIMIT 1
            """,
            [advice_id, account_id],
        ).fetchone()
    else:
        row = conn.execute(
            """
            SELECT 1 FROM paper_trades
            WHERE advice_id = ? AND reject_reason IS NULL LIMIT 1
            """,
            [advice_id],
        ).fetchone()
    return row is not None


def execute_paper_order(
    account_id: str,
    *,
    symbol: str,
    side: Side,
    shares: int,
    trade_date: dt.date,
    advice_id: str | None = None,
    source: FillSource = "sim",
    price_override: float | None = None,
    cost_assumptions: CostAssumptions | None = None,
) -> PaperOrderResult:
    """模拟一笔成交。shares 必须为 100 的整数倍。"""
    if shares <= 0 or shares % 100 != 0:
        return PaperOrderResult("rejected", None, "股数须为 100 的正整数倍", None, None, None)

    with write_session(init=True) as conn:
        if _advice_already_filled(conn, advice_id, account_id):
            return PaperOrderResult("skipped", None, "advice_id 已成交（幂等）", None, None, get_cash(conn, account_id))

        tr = _tradability_row(conn, symbol, trade_date)
        open_px = price_override if price_override is not None else tr.get("open")
        if open_px is None:
            # 信号日晚间常见：exec_date 尚无行情。按需求 R4：待执行，不算拒绝（亦不落假成交）。
            return PaperOrderResult(
                "pending",
                None,
                "执行日尚无开盘价（待开盘后再练）",
                None,
                None,
                get_cash(conn, account_id),
            )
        exec_px = float(open_px) if price_override is not None else _apply_slippage(side, float(open_px))

        if side == "buy":
            reason = _reject_buy(tr, exec_px)
        else:
            reason = _reject_sell(tr, exec_px)
        if reason:
            tid = _new_id("rej")
            conn.execute(
                """
                INSERT INTO paper_trades
                (trade_id, account_id, advice_id, symbol, side, shares, price, notional, fees,
                 trade_date, source, reject_reason)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [tid, account_id, advice_id, symbol, side, float(shares), exec_px, 0.0, 0.0, trade_date, source, reason],
            )
            return PaperOrderResult("rejected", tid, reason, exec_px, 0.0, get_cash(conn, account_id))

        if side == "sell":
            sellable = _sellable_shares(conn, account_id, symbol, trade_date)
            if shares > sellable:
                tid = _new_id("rej")
                conn.execute(
                    """
                    INSERT INTO paper_trades
                    (trade_id, account_id, advice_id, symbol, side, shares, price, notional, fees,
                     trade_date, source, reject_reason)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    [
                        tid,
                        account_id,
                        advice_id,
                        symbol,
                        side,
                        float(shares),
                        exec_px,
                        0.0,
                        0.0,
                        trade_date,
                        source,
                        "T+1 可卖数量不足",
                    ],
                )
                return PaperOrderResult("rejected", tid, "T+1 可卖数量不足", exec_px, 0.0, get_cash(conn, account_id))

        exchange = _exchange_for(conn, symbol)
        notional = exec_px * shares
        fees = apply_costs(notional, side, exchange, cost_assumptions)
        cash = get_cash(conn, account_id)

        if side == "buy":
            total_out = notional + fees
            if cash + 1e-6 < total_out:
                tid = _new_id("rej")
                conn.execute(
                    """
                    INSERT INTO paper_trades
                    (trade_id, account_id, advice_id, symbol, side, shares, price, notional, fees,
                     trade_date, source, reject_reason)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    [
                        tid,
                        account_id,
                        advice_id,
                        symbol,
                        side,
                        float(shares),
                        exec_px,
                        notional,
                        fees,
                        trade_date,
                        source,
                        "现金不足",
                    ],
                )
                return PaperOrderResult("rejected", tid, "现金不足", exec_px, fees, cash)
            new_cash = cash - total_out
            pos = conn.execute(
                "SELECT shares, avg_cost FROM paper_positions WHERE account_id = ? AND symbol = ?",
                [account_id, symbol],
            ).fetchone()
            if pos:
                old_sh, old_cost = float(pos[0]), float(pos[1])
                new_sh = old_sh + shares
                avg = (old_sh * old_cost + notional + fees) / new_sh
                conn.execute(
                    """
                    UPDATE paper_positions SET shares = ?, avg_cost = ?, updated_at = current_timestamp
                    WHERE account_id = ? AND symbol = ?
                    """,
                    [new_sh, avg, account_id, symbol],
                )
            else:
                avg = (notional + fees) / shares
                conn.execute(
                    """
                    INSERT INTO paper_positions (account_id, symbol, shares, avg_cost, opened_at)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    [account_id, symbol, float(shares), avg, trade_date],
                )
        else:
            proceeds = notional - fees
            new_cash = cash + proceeds
            pos = conn.execute(
                "SELECT shares FROM paper_positions WHERE account_id = ? AND symbol = ?",
                [account_id, symbol],
            ).fetchone()
            old_sh = float(pos[0])
            new_sh = old_sh - shares
            if new_sh <= 1e-9:
                conn.execute(
                    "DELETE FROM paper_positions WHERE account_id = ? AND symbol = ?",
                    [account_id, symbol],
                )
            else:
                conn.execute(
                    """
                    UPDATE paper_positions SET shares = ?, updated_at = current_timestamp
                    WHERE account_id = ? AND symbol = ?
                    """,
                    [new_sh, account_id, symbol],
                )

        trade_id = _new_id("fill")
        conn.execute(
            """
            INSERT INTO paper_trades
            (trade_id, account_id, advice_id, symbol, side, shares, price, notional, fees,
             trade_date, source, reject_reason)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL)
            """,
            [
                trade_id,
                account_id,
                advice_id,
                symbol,
                side,
                float(shares),
                exec_px,
                notional,
                fees,
                trade_date,
                source,
            ],
        )
        conn.execute(
            "UPDATE paper_cash SET cash_cny = ?, updated_at = current_timestamp WHERE account_id = ?",
            [new_cash, account_id],
        )
        return PaperOrderResult("filled", trade_id, None, exec_px, fees, new_cash)


def latest_paper_account_id(conn, *, kind: str = "human") -> str | None:
    """默认只返回人手练习账户，避免影子仓冒充 UI 当前账户。"""
    row = conn.execute(
        """
        SELECT account_id FROM paper_account
        WHERE coalesce(kind, 'human') = ?
        ORDER BY created_at DESC LIMIT 1
        """,
        [kind or "human"],
    ).fetchone()
    return row[0] if row else None


def last_quote_date(conn, as_of: dt.date | None = None) -> dt.date | None:
    """仓库中 <= as_of 的最近有行情交易日（周末/节假日回退）。"""
    end = as_of or dt.date.today()
    row = conn.execute(
        "SELECT MAX(trade_date) FROM daily_quotes WHERE trade_date <= ?",
        [end],
    ).fetchone()
    if not row or row[0] is None:
        return None
    d = row[0]
    return d if isinstance(d, dt.date) else pd.Timestamp(d).date()


def mark_to_market_nav(conn, account_id: str, as_of: dt.date) -> dict[str, float]:
    """现金 + 持仓按 as_of 当日或之前最近收盘价市值（缺行情则跳过该标的）。"""
    cash = get_cash(conn, account_id)
    adj = _price_adjust()
    pos = list_paper_positions(conn, account_id)
    mv = 0.0
    for _, row in pos.iterrows():
        px_row = conn.execute(
            """
            SELECT close FROM daily_quotes
            WHERE symbol = ? AND trade_date <= ? AND adjust = ?
            ORDER BY trade_date DESC LIMIT 1
            """,
            [row["symbol"], as_of, adj],
        ).fetchone()
        if px_row and px_row[0] is not None:
            mv += float(row["shares"]) * float(px_row[0])
    return {"cash_cny": cash, "market_value_cny": mv, "nav_cny": cash + mv}


_SIM_ACTION_ORDER = {"stop_loss": 0, "reduce": 1, "take_profit": 2, "rebalance": 3, "open": 4}
_SIM_BUY = {"open"}
_SIM_SELL = {"stop_loss", "reduce", "take_profit", "rebalance"}


@dataclass(frozen=True)
class AdviceSimLine:
    advice_id: str
    symbol: str
    action: str
    status: str
    message: str | None


@dataclass(frozen=True)
class AdviceSimSummary:
    filled: int
    skipped: int
    rejected: int
    pending: int
    nav_before: float
    nav_after: float
    lines: tuple[AdviceSimLine, ...]

    def reason_counts(self) -> dict[str, int]:
        """按 message 汇总（UI 原因分布）。"""
        out: dict[str, int] = {}
        for ln in self.lines:
            key = ln.message or ln.status
            out[key] = out.get(key, 0) + 1
        return dict(sorted(out.items(), key=lambda kv: (-kv[1], kv[0])))


def simulate_advice_cards(
    account_id: str,
    cards: list[dict],
    *,
    as_of_nav: dt.date | None = None,
) -> AdviceSimSummary:
    """按建议卡片批量模拟成交（幂等 advice_id；先卖后买）。M9/M3 闭环入口。

    缺执行日开盘价 → pending（不算拒绝）。见 requirements-20260918 R4。
    """
    from advice.entry_rules import entry_blocked_at_open
    from common.db import get_connection, init_schema

    nav_day = as_of_nav or dt.date.today()
    with write_session(init=True) as conn:
        nav_before = mark_to_market_nav(conn, account_id, nav_day)["nav_cny"]

    actionable = [
        c
        for c in cards
        if c.get("action") in _SIM_BUY | _SIM_SELL and int(c.get("size_shares") or 0) >= 100
    ]
    actionable.sort(key=lambda c: _SIM_ACTION_ORDER.get(c["action"], 9))

    read_conn = get_connection()
    init_schema(read_conn)
    lines: list[AdviceSimLine] = []
    filled = skipped = rejected = pending = 0
    try:
        for card in actionable:
            aid = str(card.get("advice_id") or "")
            sym = card["symbol"]
            action = card["action"]
            shares = int(card["size_shares"])
            exec_s = card.get("exec_date")
            if not exec_s:
                lines.append(AdviceSimLine(aid, sym, action, "skipped", "无 exec_date"))
                skipped += 1
                continue
            from advice.advice_engine import parse_as_of_date

            trade_date = (
                parse_as_of_date(exec_s) if not isinstance(exec_s, dt.date) else exec_s
            )
            if action == "open":
                blocked, why = entry_blocked_at_open(read_conn, sym, trade_date)
                if blocked:
                    lines.append(AdviceSimLine(aid, sym, action, "skipped", why))
                    skipped += 1
                    continue
            side: Side = "buy" if action in _SIM_BUY else "sell"
            r = execute_paper_order(
                account_id,
                symbol=sym,
                side=side,
                shares=shares,
                trade_date=trade_date,
                advice_id=aid or None,
                source="sim",
            )
            if r.status == "filled":
                filled += 1
                lines.append(
                    AdviceSimLine(aid, sym, action, "filled", f"价 {r.price:.2f}" if r.price else None)
                )
            elif r.status == "pending":
                pending += 1
                lines.append(AdviceSimLine(aid, sym, action, "pending", r.reject_reason))
            elif r.status == "skipped":
                skipped += 1
                lines.append(AdviceSimLine(aid, sym, action, "skipped", r.reject_reason))
            else:
                rejected += 1
                lines.append(AdviceSimLine(aid, sym, action, "rejected", r.reject_reason))
    finally:
        read_conn.close()

    with write_session(init=True) as conn:
        nav_after = mark_to_market_nav(conn, account_id, nav_day)["nav_cny"]

    return AdviceSimSummary(filled, skipped, rejected, pending, nav_before, nav_after, tuple(lines))


def paper_weekly_report(conn, account_id: str, *, as_of: dt.date | None = None) -> dict:
    """模拟盘近 7 日摘要（S6）：成交、换手、最大单票权重、model_run_id。"""
    end = as_of or dt.date.today()
    start = end - dt.timedelta(days=7)
    trades = conn.execute(
        """
        SELECT symbol, side, shares, price, notional, fees, trade_date, source
        FROM paper_trades
        WHERE account_id = ? AND reject_reason IS NULL AND trade_date BETWEEN ? AND ?
        """,
        [account_id, start, end],
    ).df()
    nav = mark_to_market_nav(conn, account_id, end)
    model_run = None
    try:
        row = conn.execute(
            "SELECT model_run_id FROM advice_log WHERE model_run_id IS NOT NULL ORDER BY as_of DESC LIMIT 1"
        ).fetchone()
        model_run = row[0] if row else None
    except Exception:
        pass
    if model_run is None:
        try:
            import json
            from pathlib import Path

            p = Path("mlops/registry/champion.json")
            if p.exists():
                model_run = json.loads(p.read_text(encoding="utf-8")).get("run_id")
        except Exception:
            pass

    turnover = float(trades["notional"].sum()) if not trades.empty else 0.0
    max_weight = 0.0
    if nav["nav_cny"] > 0 and not list_paper_positions(conn, account_id).empty:
        for _, row in list_paper_positions(conn, account_id).iterrows():
            px = conn.execute(
                """
                SELECT close FROM daily_quotes
                WHERE symbol = ? AND trade_date <= ? AND adjust = ?
                ORDER BY trade_date DESC LIMIT 1
                """,
                [row["symbol"], end, _price_adjust()],
            ).fetchone()
            if px and px[0]:
                max_weight = max(max_weight, float(row["shares"]) * float(px[0]) / nav["nav_cny"])

    nav_start = mark_to_market_nav(conn, account_id, start)["nav_cny"]
    ret = (nav["nav_cny"] / nav_start - 1) if nav_start > 0 else None

    return {
        "period_start": start.isoformat(),
        "period_end": end.isoformat(),
        "nav_cny": nav["nav_cny"],
        "return_7d": ret,
        "trade_count": len(trades),
        "turnover_notional": turnover,
        "max_single_weight": round(max_weight, 4),
        "model_run_id": model_run,
    }
