"""
Phase 4 建议卡片引擎：融合掘金模型打分 + 持仓状态 + 风险约束 + 行为金融冲突提示，
输出对齐 `docs/07-产品设计启示/02-场景化建议引擎.md` schema 的建议卡片。

**决策优先级**（严格按 `docs/04-风险管理/03-场景化决策建议框架.md` 第4节实现，不是随意
if/else堆砌）：
    1. 合规与可交易性（停牌/涨停封死买不进/跌停封死卖不出）
    2. 生存风控（止损/单票集中度超限）
    3. 账户结构健康（持仓状态转移）
    4. 信号质量（模型打分/因子/行为金融）
    5. 收益增强（加仓/再平衡）
"观望"是一等动作（无强信号也无风险时，不强行输出建议）。

**关于"action的动作空间"**：本模块只产出**信息性建议**（reduce/hold/watch/open/
stop_loss/take_profit），**不自动下单**——严格遵守
`docs/00-总览/02-产品定位与边界.md` 的Won't清单，用户需要自行在券商App操作。

**止盈止损阈值来源**：复用生产冠军模型训练时的三重障碍标签配置(`profit_take=0.08,
stop_loss=0.08`，见 `mlops/registry/20260826_123439/metadata.json` 的
`label_config`)——模型本身就是按"8%止盈/8%止损/20日"的标签训练出来的，建议卡片用
同样的阈值判断持仓，逻辑自洽（不是另外拍一个不相关的数字）。

**再平衡（rebalance）覆盖层**：`risk/portfolio_optimizer.py` 输出的目标权重只用于
"优先级5：收益增强"——只会把已经判定为 `hold`（即前4级更高优先级规则都没有触发）的
卡片，在权重明显偏离(|diff|>阈值)优化目标时升级为 `rebalance`，绝不会覆盖止损/减仓
等更高优先级的风控结论，严格对齐类文档docstring开头的优先级列表。
"""
from __future__ import annotations

import datetime as dt
import json
import logging
import uuid

import pandas as pd

from advice.reason_pack import build_reason_pack

logger = logging.getLogger("advice.advice_engine")

_STOP_LOSS_PCT = -0.08
_TAKE_PROFIT_PCT = 0.08
_SINGLE_NAME_LIMIT = 0.15  # 单票占净值上限，超过触发"减仓"风控（对齐risk/portfolio_risk.py同一阈值）
_OPEN_CANDIDATE_RANK_THRESHOLD = 50  # 排名进入前50才考虑"建仓"级别的建议，其余只是"观察"


def _reasons_from_factors(row: pd.Series) -> list[dict]:
    """S1：因子 + 行为冲突同一条 reasons 链；行为项不改排名。"""
    return build_reason_pack(row)["reasons"]


def _risks_from_behavior(row: pd.Series) -> list[str]:
    risks = []
    pack = build_reason_pack(row)
    if pack["has_conflict"]:
        for c in pack["conflicts"]:
            risks.append(c["detail"])
    return risks


def _plain_summary(action: str, **kw) -> str:
    """给非专业用户的大白话一句话总结，跟`reasons`/`risks`里的专业措辞分开——
    UI优化需求"缺少更直观易懂的说明"的直接回应。不改变action判定逻辑本身，
    只是把已经算好的结论翻译成日常语言。"""
    if action == "stop_loss":
        return f"已经亏了{kw.get('pnl_pct', 0):.1%}，跌破止损线了，建议尽快考虑卖出止损，别让亏损继续扩大。"
    if action == "reduce":
        return f"这只股票占你总仓位的{kw.get('weight', 0):.1%}，太集中了，建议卖出一部分分散风险，鸡蛋不要放一个篮子里。"
    if action == "take_profit":
        return f"已经赚了{kw.get('pnl_pct', 0):.1%}，而且模型现在没那么看好了，可以考虑先卖一部分锁定收益。"
    if action == "rebalance":
        return "当前持仓比例与系统建议比例偏差较大，可考虑小幅调整；紧急程度通常低于止损与减仓。"
    if action == "open":
        rank = kw.get("rank")
        sh = kw.get("size_shares")
        if sh and int(sh) >= 100:
            amt = kw.get("est_amount_cny") or 0
            stop = kw.get("stop")
            ml = kw.get("max_loss")
            head = (
                f"模型在全市场排第{int(rank)}名，建议以损定仓买入约 {int(sh)} 股"
                f"（约 ¥{float(amt):.0f}）"
                if rank
                else f"建议以损定仓买入约 {int(sh)} 股（约 ¥{float(amt):.0f}）"
            )
            if stop is not None:
                head += f"，参考止损 ¥{float(stop):.2f}"
            if ml is not None:
                head += f"，若触发止损最大亏约 ¥{float(ml):.0f}"
            return head + "。"
        return (
            f"模型在全市场排到第{int(rank)}名（越靠前表示排序越靠前），若感兴趣可进一步研究是否建仓。"
            if rank
            else "模型排序相对靠前，可进一步研究。"
        )
    if action == "hold":
        if kw.get("rank_ok"):
            return "模型仍相对看好这只股票，暂无强制动作，可继续持有并观察。"
        return "目前没有特别强的信号，可按原计划持有观察，避免频繁操作。"
    if action == "watch":
        if kw.get("not_tradable"):
            return "今日无法交易（停牌或涨跌停封死），请等待可交易后再决定。"
        return "暂时没有特别强的信号，建议先观望。"
    return ""


def price_levels(last_close: float | None, cost_price: float | None = None) -> dict:
    """把百分比阈值换算成人民币具体价格，方便用户直接对照行情软件操作，
    不用自己心算"跌8%是多少钱"。基准价用成本价（有持仓时）或现价（未持仓时）。
    公开导出（非下划线私有），供 `pages/5_行情图表.py` 复用同一套阈值换算，
    不重复实现（对齐vibe coding八荣八耻第4条"复用存量"）。"""
    base = cost_price if cost_price is not None else last_close
    levels = {"last_close": last_close, "cost_price": cost_price}
    if base is not None:
        levels["stop_loss_price"] = round(base * (1 + _STOP_LOSS_PCT), 2)
        levels["take_profit_price"] = round(base * (1 + _TAKE_PROFIT_PCT), 2)
    else:
        levels["stop_loss_price"] = None
        levels["take_profit_price"] = None
    return levels


def build_advice_for_watchlist(scan_top: pd.DataFrame) -> list[dict]:
    """未持有、但在掘金Top候选里的股票 -> watch/open建议。scan_top 来自
    `advice/scanner.py::run_scan()` 的 top_pick 返回值（已含behavior信号列）。"""
    from advice.position_sizing import actionable_invalid_if_open, actionable_invalid_if_watch

    cards = []
    for _, row in scan_top.iterrows():
        not_tradable = not row.get("is_tradable", True)
        if not_tradable:
            action, confidence = "watch", 0.0
            risks = ["当前不可交易（停牌或涨停封死）"]
        elif row["rank"] <= _OPEN_CANDIDATE_RANK_THRESHOLD:
            action = "open"
            confidence = round(max(0.0, 1.0 - row["rank"] / _OPEN_CANDIDATE_RANK_THRESHOLD), 3)
            risks = _risks_from_behavior(row)
        else:
            action, confidence = "watch", 0.0
            risks = _risks_from_behavior(row)

        last_close = row.get("close")
        pack = build_reason_pack(row)
        pl = price_levels(float(last_close) if pd.notna(last_close) else None)
        if action == "open":
            inv = actionable_invalid_if_open(pl.get("stop_loss_price"))
        else:
            inv = actionable_invalid_if_watch()
        cards.append({
            "advice_id": str(uuid.uuid4()),
            "symbol": row["symbol"],
            "name": row.get("name"),
            "rank": int(row["rank"]) if pd.notna(row.get("rank")) else None,
            "action": action,
            "confidence": confidence,
            "price_levels": pl,
            "plain_summary": _plain_summary(action, rank=row.get("rank"), not_tradable=not_tradable),
            "reason_one_liner": pack["one_liner"],
            "reasons": pack["reasons"],
            "risks": risks,
            "invalid_if": inv,
            "constraints_applied": ["Tplus1", "涨跌停/停牌过滤"],
            "disclaimer": "仅供研究辅助，不构成投资建议",
        })
    return cards


def build_advice_for_positions(
    position_summary: pd.DataFrame,
    scan_full: pd.DataFrame,
    concentration: pd.DataFrame,
) -> list[dict]:
    """已持有的股票 -> hold/reduce/stop_loss/take_profit/watch 建议（本轮不产出 add）。
    position_summary 来自 `risk/portfolio_risk.py::compute_position_summary()`
    （逐lot，含unrealized_pnl_pct）；scan_full 是当天全量模型排名（不只Top-N，
    用于判断"是否已跌出模型认可范围"）；concentration 来自
    `risk/portfolio_risk.py::compute_concentration()`。"""
    if position_summary.empty:
        return []
    from advice.position_sizing import (
        actionable_invalid_if_hold,
        actionable_invalid_if_position,
        actionable_invalid_if_watch,
    )
    agg = position_summary.groupby("symbol", as_index=False).agg(
        shares=("shares", "sum"), cost_value=("cost_value", "sum"), market_value=("market_value", "sum"),
    )
    agg["unrealized_pnl_pct"] = agg["market_value"] / agg["cost_value"] - 1
    weight_map = concentration.set_index("symbol")["weight_pct"].to_dict() if not concentration.empty else {}
    score_map = scan_full.set_index("symbol")[["pred_score", "rank", "is_tradable"]].to_dict("index") if not scan_full.empty else {}
    name_map = scan_full.set_index("symbol")["name"].to_dict() if not scan_full.empty and "name" in scan_full.columns else {}
    cost_map = position_summary.groupby("symbol")["cost_price"].mean().to_dict()
    last_close_map = position_summary.groupby("symbol")["last_close"].last().to_dict()

    cards = []
    for _, row in agg.iterrows():
        symbol = row["symbol"]
        weight = weight_map.get(symbol, 0.0)
        model_info = score_map.get(symbol, {})
        pnl_pct = row["unrealized_pnl_pct"]

        # 优先级1：可交易性
        if model_info and model_info.get("is_tradable") is False:
            action, confidence = "watch", 0.0
            risks = ["当前不可交易（停牌或涨停/跌停封死），暂无法操作"]
        # 优先级2：生存风控——止损线触发，且不可交易性未拦截
        elif pnl_pct <= _STOP_LOSS_PCT:
            action, confidence = "stop_loss", 0.8
            risks = [f"浮亏{pnl_pct:.1%}已触及止损线({_STOP_LOSS_PCT:.0%})"]
        # 优先级2：生存风控——单票集中度超限
        elif weight > _SINGLE_NAME_LIMIT:
            action, confidence = "reduce", 0.6
            risks = [f"单票占净值{weight:.1%}，超过{_SINGLE_NAME_LIMIT:.0%}上限"]
        # 优先级4：信号质量——止盈线触发 且 模型排名已经不再靠前(信号减弱才建议兑现，
        # 避免"一涨就卖"的过度止盈)
        elif pnl_pct >= _TAKE_PROFIT_PCT and (not model_info or model_info.get("rank", 0) > _OPEN_CANDIDATE_RANK_THRESHOLD * 2):
            action, confidence = "take_profit", 0.6
            risks = [f"浮盈{pnl_pct:.1%}已达止盈参考线({_TAKE_PROFIT_PCT:.0%})，且模型排名已走弱"]
        elif model_info and model_info.get("rank", 10**9) <= _OPEN_CANDIDATE_RANK_THRESHOLD:
            action, confidence = "hold", round(max(0.0, 1.0 - model_info["rank"] / _OPEN_CANDIDATE_RANK_THRESHOLD), 3)
            risks = []
        else:
            action, confidence = "hold", 0.3
            risks = ["模型当前排名未进入前列，信号强度中性" if model_info else "无最新模型打分"]

        reasons = [{"type": "position", "ref": "unrealized_pnl", "detail": f"当前浮盈亏 {pnl_pct:.1%}"}]
        if model_info and pd.notna(model_info.get("pred_score")):
            reasons.append({"type": "model", "ref": "champion_model",
                             "detail": f"模型打分 {model_info['pred_score']:.4f}，排名第{int(model_info['rank'])}"})

        last_close = last_close_map.get(symbol)
        cost_price = cost_map.get(symbol)
        pl = price_levels(
            float(last_close) if pd.notna(last_close) else None,
            float(cost_price) if pd.notna(cost_price) else None,
        )

        if action == "watch":
            inv = actionable_invalid_if_watch()
        elif action == "hold":
            inv = actionable_invalid_if_hold(stop_price=pl.get("stop_loss_price"))
        else:
            inv = actionable_invalid_if_position(
                action,
                stop_price=pl.get("stop_loss_price"),
                take_profit_price=pl.get("take_profit_price"),
            )
        cards.append({
            "advice_id": str(uuid.uuid4()),
            "symbol": symbol,
            "name": name_map.get(symbol),
            "action": action,
            "confidence": confidence,
            "size_pct_nav": [0.0, round(weight, 4)],
            "price_levels": pl,
            "plain_summary": _plain_summary(
                action, pnl_pct=pnl_pct, weight=weight,
                rank_ok=bool(model_info and model_info.get("rank", 10**9) <= _OPEN_CANDIDATE_RANK_THRESHOLD),
                not_tradable=bool(model_info and model_info.get("is_tradable") is False),
            ),
            "reasons": reasons,
            "risks": risks,
            "invalid_if": inv,
            "constraints_applied": ["Tplus1", "单票集中度上限", "涨跌停/停牌过滤"],
            "disclaimer": "仅供研究辅助，不构成投资建议",
        })
    return cards


def apply_rebalance_overlay(
    position_cards: list[dict],
    rebalance_df: pd.DataFrame,
    drift_threshold: float = 0.03,
) -> list[dict]:
    """优先级5"收益增强"覆盖层：只把当前动作是`hold`（意味着更高优先级规则都判定为
    "无风险无强信号"）的卡片，在组合优化目标权重与当前权重偏离超过 `drift_threshold`
    时升级为 `rebalance`；`reduce`/`stop_loss`/`take_profit`/`watch` 等已有更高优先级
    结论的卡片保持不变——严格不越权覆盖风控结论。`rebalance_df` 来自
    `risk/portfolio_optimizer.py::suggest_rebalance()`，为空(数据不足/优化未收敛)时
    原样返回，不报错中断。"""
    if rebalance_df is None or rebalance_df.empty:
        return position_cards
    diff_map = rebalance_df.set_index("symbol")["diff"].to_dict()
    hint_map = rebalance_df.set_index("symbol")["action_hint"].to_dict()

    out = []
    for card in position_cards:
        if card["action"] != "hold":
            out.append(card)
            continue
        diff = diff_map.get(card["symbol"])
        hint = hint_map.get(card["symbol"])
        if diff is None or abs(diff) < drift_threshold:
            out.append(card)
            continue
        direction = "上调至" if hint == "increase" else "下调至"
        card = dict(card)
        card["action"] = "rebalance"
        card["confidence"] = round(min(0.5, abs(diff)), 3)
        card["size_pct_nav"] = [round(diff, 4), card.get("size_pct_nav", [0.0, 0.0])[1]]
        card["reasons"] = card["reasons"] + [{
            "type": "portfolio_optimization", "ref": "mean_variance_ledoit_wolf",
            "detail": f"均值-方差组合优化建议将该标的权重{direction}约{abs(diff):.1%}（Grinold-Kahn精化Alpha + "
                      f"Ledoit-Wolf协方差收缩，详见risk/portfolio_optimizer.py）",
        }]
        out.append(card)
    return out


def _advice_equity_and_cash(conn) -> tuple[float, float]:
    from advice.paper_broker import mark_to_market_nav

    row = conn.execute(
        "SELECT account_id FROM paper_account ORDER BY created_at DESC LIMIT 1"
    ).fetchone()
    if row:
        nav = mark_to_market_nav(conn, row[0], dt.date.today())
        return float(nav["nav_cny"]), float(nav["cash_cny"])
    from advice.position_sizing import default_equity_cny, sizing_params

    eq = default_equity_cny()
    buf = sizing_params()["min_cash_buffer"]
    return eq, eq * (1.0 - buf)


def enrich_cards_with_sizing(
    conn,
    cards: list[dict],
    signal_date: dt.date,
    position_summary: pd.DataFrame,
    held_symbols: set[str],
    exchange_by_symbol: dict[str, str],
) -> list[dict]:
    """Phase 5 阶段 B：为卡片填充股数/金额/失效条件；买不起 1 手则 open→watch。"""
    from advice.entry_rules import next_trade_date
    from advice.paper_broker import _exchange_for
    from advice.position_sizing import (
        actionable_invalid_if_hold,
        actionable_invalid_if_open,
        actionable_invalid_if_position,
        actionable_invalid_if_watch,
        compute_open_size,
        compute_sell_shares,
        invalid_if_is_actionable,
        sizing_params,
    )

    p = sizing_params()
    exec_d = next_trade_date(conn, signal_date)
    exec_iso = exec_d.isoformat() if exec_d else None
    equity, cash = _advice_equity_and_cash(conn)

    shares_map: dict[str, float] = {}
    weight_map: dict[str, float] = {}
    if not position_summary.empty:
        agg = position_summary.groupby("symbol", as_index=False).agg(
            shares=("shares", "sum"),
            market_value=("market_value", "sum"),
        )
        total_mv = float(agg["market_value"].sum())
        for _, r in agg.iterrows():
            shares_map[r["symbol"]] = float(r["shares"])
            weight_map[r["symbol"]] = (
                float(r["market_value"]) / total_mv if total_mv > 0 else 0.0
            )

    rank_by_symbol: dict[str, float] = {}
    for c in cards:
        for reason in c.get("reasons") or []:
            if reason.get("type") == "model" and "排名第" in reason.get("detail", ""):
                try:
                    tail = reason["detail"].split("排名第")[1]
                    rank_by_symbol[c["symbol"]] = float(tail.split("名")[0])
                except (IndexError, ValueError):
                    pass

    out: list[dict] = []
    for card in cards:
        c = dict(card)
        c["horizon_days"] = p["horizon_days"]
        c["exec_date"] = exec_iso
        action = c["action"]
        sym = c["symbol"]
        pl = c.get("price_levels") or {}
        last_close = pl.get("last_close")
        if last_close is None or (isinstance(last_close, float) and pd.isna(last_close)):
            if not invalid_if_is_actionable(c.get("invalid_if")):
                act = c.get("action")
                if act == "watch":
                    c["invalid_if"] = actionable_invalid_if_watch()
                elif act == "hold":
                    c["invalid_if"] = actionable_invalid_if_hold(stop_price=pl.get("stop_loss_price"))
                elif act == "open":
                    c["invalid_if"] = actionable_invalid_if_open(pl.get("stop_loss_price"))
            out.append(c)
            continue
        price = float(last_close)
        exchange = exchange_by_symbol.get(sym) or _exchange_for(conn, sym)

        if action == "open":
            if len(held_symbols) >= p["max_concurrent_positions"]:
                c["action"] = "watch"
                c["confidence"] = 0.0
                c["risks"] = list(c.get("risks") or []) + [
                    f"已达最大同时持仓数 {p['max_concurrent_positions']}，暂不建议新开仓"
                ]
                c["plain_summary"] = _plain_summary("watch")
                c["invalid_if"] = actionable_invalid_if_watch()
                out.append(c)
                continue
            stop = pl.get("stop_loss_price")
            if stop is None:
                c["action"] = "watch"
                c["confidence"] = 0.0
                c["size_shares"] = 0
                c["risks"] = list(c.get("risks") or []) + ["缺少参考止损价，无法以损定仓"]
                c["plain_summary"] = _plain_summary("watch")
                c["invalid_if"] = actionable_invalid_if_watch()
                out.append(c)
                continue
            sz = compute_open_size(equity, price, float(stop), exchange, cash_cny=cash)
            if sz.shares < 100:
                c["action"] = "watch"
                c["confidence"] = 0.0
                c["risks"] = list(c.get("risks") or []) + [sz.reason or "以损定仓后无法买入 1 手"]
                c["plain_summary"] = _plain_summary("watch")
                c["size_shares"] = 0
                c["invalid_if"] = actionable_invalid_if_watch()
            else:
                c["size_shares"] = sz.shares
                c["est_amount_cny"] = sz.est_amount_cny
                c["size_pct_nav"] = sz.size_pct_nav
                c["max_loss_cny"] = sz.max_loss_cny
                c["invalid_if"] = actionable_invalid_if_open(pl.get("stop_loss_price"))
                c["plain_summary"] = _plain_summary(
                    "open",
                    rank=c.get("rank") or rank_by_symbol.get(sym),
                    size_shares=sz.shares,
                    est_amount_cny=sz.est_amount_cny,
                    stop=stop,
                    max_loss=sz.max_loss_cny,
                )
        elif action in ("stop_loss", "reduce", "take_profit", "rebalance"):
            held = shares_map.get(sym, 0.0)
            w = weight_map.get(sym, 0.0)
            sz = compute_sell_shares(
                held, action, equity_cny=equity, price=price, current_weight=w
            )
            if sz.shares >= 100:
                c["size_shares"] = sz.shares
                c["est_amount_cny"] = sz.est_amount_cny
                if isinstance(c.get("size_pct_nav"), list):
                    c["size_pct_nav"] = [round(sz.size_pct_nav, 4), c["size_pct_nav"][1]]
                else:
                    c["size_pct_nav"] = sz.size_pct_nav
            c["invalid_if"] = actionable_invalid_if_position(
                action,
                stop_price=pl.get("stop_loss_price"),
                take_profit_price=pl.get("take_profit_price"),
            )
        if not invalid_if_is_actionable(c.get("invalid_if")):
            act = c.get("action")
            if act == "open":
                c["invalid_if"] = actionable_invalid_if_open(pl.get("stop_loss_price"))
            elif act == "watch":
                c["invalid_if"] = actionable_invalid_if_watch()
            elif act == "hold":
                c["invalid_if"] = actionable_invalid_if_hold(stop_price=pl.get("stop_loss_price"))
            elif act in ("stop_loss", "reduce", "take_profit", "rebalance"):
                c["invalid_if"] = actionable_invalid_if_position(
                    act,
                    stop_price=pl.get("stop_loss_price"),
                    take_profit_price=pl.get("take_profit_price"),
                )
        out.append(c)
    return out


_POSITION_ACTIONS = {"stop_loss", "reduce", "take_profit", "rebalance", "hold"}
_PRIORITY_ORDER = ["stop_loss", "reduce", "take_profit", "rebalance", "open", "hold", "watch"]


def cards_for_digest(cards: list[dict], held_symbols: set[str]) -> list[dict]:
    """首页速览用：持仓类动作（止损/减仓/持有等）只保留「当前仍持有」的股票。
    平仓后即使还没再点生成，旧风控卡片也不应继续催你操作已经卖掉的票。"""
    out = []
    for c in cards:
        action = c.get("action")
        if action in _POSITION_ACTIONS:
            if c.get("symbol") in held_symbols:
                out.append(c)
        else:
            out.append(c)
    return out


def build_priority_digest(position_cards: list[dict], watchlist_cards: list[dict], top_n: int = 8) -> list[dict]:
    """首页「今日决策速览」用：把持仓建议+观察名单建议按优先级(止损>减仓>止盈>再平衡>
    建仓>持有>观望)排序，只挑最需要用户关注的前`top_n`条——不重新判定任何action，
    只做展示层的排序聚合，判定逻辑仍然全部来自上面的build_advice_for_*。"""
    all_cards = list(position_cards) + list(watchlist_cards)

    def _key(c: dict) -> tuple:
        try:
            pr = _PRIORITY_ORDER.index(c["action"])
        except ValueError:
            pr = len(_PRIORITY_ORDER)
        return (pr, -c.get("confidence", 0.0))

    urgent_actions = {"stop_loss", "reduce", "take_profit", "rebalance"}
    ranked = sorted(all_cards, key=_key)
    # watch动作除非top_n还有余量否则不挤占速览位置（观望不是"需要行动"的事）
    priority_first = [c for c in ranked if c["action"] != "watch"]
    filler = [c for c in ranked if c["action"] == "watch"]
    result = (priority_first + filler)[:top_n]
    for c in result:
        c["is_urgent"] = c["action"] in urgent_actions
    return result


def persist_advice_cards(conn, cards: list[dict], as_of_date: str, model_run_id: str | None = None) -> None:
    """同一交易日的建议是一份快照：先删掉该日全部旧行，再写入本次结果。

    只按 (symbol, as_of) upsert 不够——平仓后的股票不会出现在新卡片里，旧的止损/减仓
    行会留在库里，首页「今日决策速览」就会只叠加、不消失（2026-08-26 第二轮人工测试复现）。
    """
    as_of = pd.Timestamp(as_of_date).date()
    conn.execute("DELETE FROM advice_log WHERE as_of = ?", [as_of])
    if not cards:
        return
    from common.tushare_client import upsert

    rows = []
    for c in cards:
        rows.append({
            "advice_id": c["advice_id"],
            "as_of": as_of,
            "symbol": c["symbol"],
            "name": c.get("name"),
            "action": c["action"],
            "confidence": c.get("confidence"),
            "plain_summary": c.get("plain_summary"),
            "price_levels_json": json.dumps(c.get("price_levels", {}), ensure_ascii=False, default=str),
            "reasons_json": json.dumps(c.get("reasons", []), ensure_ascii=False),
            "risks_json": json.dumps(c.get("risks", []), ensure_ascii=False),
            "invalid_if_json": json.dumps(c.get("invalid_if", []), ensure_ascii=False),
            "reason_one_liner": c.get("reason_one_liner"),
            "model_run_id": model_run_id,
            "size_shares": c.get("size_shares"),
            "est_amount_cny": c.get("est_amount_cny"),
            "size_pct_nav": (
                c["size_pct_nav"][1]
                if isinstance(c.get("size_pct_nav"), list) and len(c["size_pct_nav"]) > 1
                else c.get("size_pct_nav")
            ),
            "max_loss_cny": c.get("max_loss_cny"),
            "horizon_days": c.get("horizon_days"),
            "exec_date": (
                pd.Timestamp(c["exec_date"]).date() if c.get("exec_date") else None
            ),
        })
    df = pd.DataFrame(rows)
    upsert(conn, "advice_log", ["symbol", "as_of"], df)


def load_latest_advice_cards(conn) -> tuple[str | None, list[dict]]:
    """从`advice_log`读回最近一次生成的建议卡片（跨Streamlit会话持久化），
    供首页「今日决策速览」使用——不需要每次打开首页都重新跑一次全市场扫描。
    返回 (as_of日期字符串或None, 卡片列表)。"""
    latest = conn.execute("SELECT MAX(as_of) AS d FROM advice_log").df()
    if latest.empty or pd.isna(latest["d"].iloc[0]):
        return None, []
    as_of = str(latest["d"].iloc[0])
    df = conn.execute("SELECT * FROM advice_log WHERE as_of = ? ORDER BY created_at DESC", [as_of]).df()
    cards = []
    for _, r in df.iterrows():
        try:
            price_levels = json.loads(r["price_levels_json"]) if r.get("price_levels_json") else {}
        except (TypeError, ValueError):
            price_levels = {}
        cards.append({
            "advice_id": r["advice_id"],
            "symbol": r["symbol"],
            "name": r.get("name"),
            "action": r["action"],
            "confidence": r.get("confidence") or 0.0,
            "plain_summary": r.get("plain_summary") or "",
            "price_levels": price_levels,
            "reasons": json.loads(r["reasons_json"]) if r.get("reasons_json") else [],
            "risks": json.loads(r["risks_json"]) if r.get("risks_json") else [],
            "invalid_if": json.loads(r["invalid_if_json"]) if r.get("invalid_if_json") else [],
            "reason_one_liner": (r.get("reason_one_liner") or "") if "reason_one_liner" in r.index else "",
            "disclaimer": "仅供研究辅助，不构成投资建议",
            "size_shares": r.get("size_shares"),
            "est_amount_cny": r.get("est_amount_cny"),
            "size_pct_nav": r.get("size_pct_nav"),
            "max_loss_cny": r.get("max_loss_cny"),
            "horizon_days": r.get("horizon_days"),
            "exec_date": str(r["exec_date"]) if pd.notna(r.get("exec_date")) else None,
        })
    return as_of, cards


def generate_daily_advice(
    top_n_watchlist: int = 30,
    *,
    allow_bj_open: bool | None = None,
) -> dict:
    """一站式入口：跑一次掘金扫描 + 读当前持仓 + 生成全部建议卡片 + 落库留痕。
    供 `pages/` Streamlit页面和 `scripts/daily_pipeline.py` 复用，不重复实现。

    allow_bj_open：None 时读 config；UI 会话可传入覆盖。
    """
    from advice.entry_rules import build_tomorrow_todos
    from advice.scanner import run_scan
    from common.db import get_connection, init_schema
    from risk.portfolio_optimizer import suggest_rebalance
    from risk.portfolio_risk import compute_concentration, compute_position_summary

    scan_full, scan_top, trade_date = run_scan(top_n=top_n_watchlist, pool_id="hs", source="advice")
    conn = get_connection()
    init_schema(conn)
    position_cards: list[dict] = []
    watchlist_cards: list[dict] = []
    tomorrow_todos: list[dict] = []
    try:
        position_summary = compute_position_summary(conn, str(trade_date))
        concentration = compute_concentration(position_summary)
        held_symbols = set(position_summary["symbol"]) if not position_summary.empty else set()

        watchlist_cards = build_advice_for_watchlist(scan_top[~scan_top["symbol"].isin(held_symbols)])
        position_cards = build_advice_for_positions(position_summary, scan_full, concentration)

        if held_symbols:
            try:
                candidate_symbols = list(held_symbols | set(scan_top["symbol"]))
                pred_scores = dict(zip(scan_full["symbol"], scan_full["pred_score"]))
                rebalance_df = suggest_rebalance(conn, concentration, candidate_symbols, pred_scores, str(trade_date))
                position_cards = apply_rebalance_overlay(position_cards, rebalance_df)
            except Exception:
                logger.exception("组合优化再平衡建议计算失败，跳过该覆盖层，不影响其余建议卡片")

        all_cards = position_cards + watchlist_cards
        exchange_map = {}
        if not scan_full.empty and "exchange" in scan_full.columns:
            exchange_map = scan_full.set_index("symbol")["exchange"].to_dict()
        signal_d = pd.Timestamp(trade_date).date()
        all_cards = enrich_cards_with_sizing(
            conn,
            all_cards,
            signal_d,
            position_summary,
            held_symbols,
            exchange_map,
        )
        position_cards = [c for c in all_cards if c["symbol"] in held_symbols]
        watchlist_cards = [c for c in all_cards if c["symbol"] not in held_symbols]
        tomorrow_todos = build_tomorrow_todos(
            all_cards, signal_d, conn, max_items=3, allow_bj_open=allow_bj_open
        )

        model_run_id = None
        try:
            with open("mlops/registry/champion_hs.json", encoding="utf-8") as f:
                model_run_id = json.load(f)["run_id"]
        except FileNotFoundError:
            try:
                with open("mlops/registry/champion.json", encoding="utf-8") as f:
                    model_run_id = json.load(f)["run_id"]
            except FileNotFoundError:
                pass
        persist_advice_cards(conn, all_cards, str(trade_date), model_run_id)
    finally:
        conn.close()

    logger.info("生成建议卡片：持仓相关%d条，观察名单%d条，交易日=%s",
                len(position_cards), len(watchlist_cards), trade_date)
    return {
        "as_of": str(trade_date),
        "position_cards": position_cards,
        "watchlist_cards": watchlist_cards,
        "tomorrow_todos": tomorrow_todos,
        "pool_id": "hs",
    }


def run_practice_cycle(
    paper_account_id: str,
    *,
    top_n_watchlist: int = 30,
    simulate_scope: str = "todos",
    allow_bj_open: bool | None = None,
) -> dict:
    """扫描 → 建议（含股数）→ 纸面批量模拟。供 UI 一键练习，不自动下单。

    simulate_scope:
      - ``todos``（默认，需求 R4）：只模拟明日待办 ≤3
      - ``all``：全部可执行卡片（UI 须二次确认）
    """
    from advice.paper_broker import simulate_advice_cards

    advice = generate_daily_advice(top_n_watchlist=top_n_watchlist, allow_bj_open=allow_bj_open)
    if simulate_scope == "all":
        cards = advice["position_cards"] + advice["watchlist_cards"]
    else:
        # 待办已是卡片子集字段；补齐 simulate 所需键
        cards = []
        for t in advice.get("tomorrow_todos") or []:
            cards.append(
                {
                    "advice_id": t.get("advice_id"),
                    "symbol": t["symbol"],
                    "name": t.get("name"),
                    "action": t["action"],
                    "size_shares": t.get("size_shares") or 0,
                    "exec_date": t.get("exec_date"),
                }
            )
    sim = simulate_advice_cards(paper_account_id, cards)
    open_top3 = [
        c
        for c in advice.get("watchlist_cards", [])
        if c.get("action") == "open" and int(c.get("size_shares") or 0) >= 100
    ][:3]
    return {
        **advice,
        "paper_account_id": paper_account_id,
        "open_top3": open_top3,
        "simulate_scope": simulate_scope,
        "simulation": {
            "filled": sim.filled,
            "skipped": sim.skipped,
            "rejected": sim.rejected,
            "pending": sim.pending,
            "nav_before": sim.nav_before,
            "nav_after": sim.nav_after,
            "reason_counts": sim.reason_counts(),
            "lines": [
                {
                    "advice_id": ln.advice_id,
                    "symbol": ln.symbol,
                    "action": ln.action,
                    "status": ln.status,
                    "message": ln.message,
                }
                for ln in sim.lines
            ],
        },
    }


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    result = generate_daily_advice()
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
