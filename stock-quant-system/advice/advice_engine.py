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

logger = logging.getLogger("advice.advice_engine")

_STOP_LOSS_PCT = -0.08
_TAKE_PROFIT_PCT = 0.08
_SINGLE_NAME_LIMIT = 0.15  # 单票占净值上限，超过触发"减仓"风控（对齐risk/portfolio_risk.py同一阈值）
_OPEN_CANDIDATE_RANK_THRESHOLD = 50  # 排名进入前50才考虑"建仓"级别的建议，其余只是"观察"


def _reasons_from_factors(row: pd.Series) -> list[dict]:
    reasons = []
    if pd.notna(row.get("pred_score")):
        reasons.append({"type": "model", "ref": "champion_model", "detail": f"模型打分 {row['pred_score']:.4f}，全市场排名第{int(row['rank'])}" if pd.notna(row.get("rank")) else f"模型打分 {row['pred_score']:.4f}"})
    if pd.notna(row.get("factor_mom_12_1")):
        reasons.append({"type": "factor", "ref": "MOM-12-1", "detail": f"12月动量(剔近1月): {row['factor_mom_12_1']:.4f}"})
    if pd.notna(row.get("factor_roe")):
        reasons.append({"type": "factor", "ref": "ROE", "detail": f"ROE: {row['factor_roe']:.4f}"})
    return reasons


def _risks_from_behavior(row: pd.Series) -> list[str]:
    risks = []
    conflict = row.get("conflict_flag")
    if conflict and conflict != "无冲突":
        risks.append(f"行为金融冲突信号: {conflict}")
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
        return "当前持仓比例和系统建议的最优比例差得有点多，可以考虑往建议方向微调，不是紧急操作。"
    if action == "open":
        rank = kw.get("rank")
        return f"模型在全市场里把它排到了第{int(rank)}名（越靠前越被看好），如果感兴趣可以进一步研究是否建仓，仓位大小自己把控。" if rank else "模型比较看好，可进一步研究。"
    if action == "hold":
        if kw.get("rank_ok"):
            return "模型仍然看好这只股票，暂时没有需要操作的地方，正常持有观察就好。"
        return "目前没有特别强的信号，正常持有、按计划观察即可，不用频繁操作。"
    if action == "watch":
        if kw.get("not_tradable"):
            return "今天这只股票没法交易（停牌或涨跌停封死），先别急，等能交易了再说。"
        return "暂时没有特别强的信号，建议先观望，不用着急下手。"
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
        cards.append({
            "advice_id": str(uuid.uuid4()),
            "symbol": row["symbol"],
            "name": row.get("name"),
            "action": action,
            "confidence": confidence,
            "price_levels": price_levels(float(last_close) if pd.notna(last_close) else None),
            "plain_summary": _plain_summary(action, rank=row.get("rank"), not_tradable=not_tradable),
            "reasons": _reasons_from_factors(row),
            "risks": risks,
            "invalid_if": ["模型下一次重新打分排名大幅下降", "行为金融冲突信号升级"],
            "constraints_applied": ["Tplus1", "涨跌停/停牌过滤"],
            "disclaimer": "仅供研究辅助，不构成投资建议",
        })
    return cards


def build_advice_for_positions(
    position_summary: pd.DataFrame,
    scan_full: pd.DataFrame,
    concentration: pd.DataFrame,
) -> list[dict]:
    """已持有的股票 -> hold/add/reduce/stop_loss/take_profit建议。
    position_summary 来自 `risk/portfolio_risk.py::compute_position_summary()`
    （逐lot，含unrealized_pnl_pct）；scan_full 是当天全量模型排名（不只Top-N，
    用于判断"是否已跌出模型认可范围"）；concentration 来自
    `risk/portfolio_risk.py::compute_concentration()`。"""
    if position_summary.empty:
        return []
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
        cards.append({
            "advice_id": str(uuid.uuid4()),
            "symbol": symbol,
            "name": name_map.get(symbol),
            "action": action,
            "confidence": confidence,
            "size_pct_nav": [0.0, round(weight, 4)],
            "price_levels": price_levels(
                float(last_close) if pd.notna(last_close) else None,
                float(cost_price) if pd.notna(cost_price) else None,
            ),
            "plain_summary": _plain_summary(
                action, pnl_pct=pnl_pct, weight=weight,
                rank_ok=bool(model_info and model_info.get("rank", 10**9) <= _OPEN_CANDIDATE_RANK_THRESHOLD),
                not_tradable=bool(model_info and model_info.get("is_tradable") is False),
            ),
            "reasons": reasons,
            "risks": risks,
            "invalid_if": [f"浮亏超过{_STOP_LOSS_PCT:.0%}", f"浮盈超过{_TAKE_PROFIT_PCT:.0%}且模型排名走弱"],
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


_PRIORITY_ORDER = ["stop_loss", "reduce", "take_profit", "rebalance", "open", "hold", "watch"]


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
    if not cards:
        return
    from common.tushare_client import upsert

    rows = []
    for c in cards:
        rows.append({
            "advice_id": c["advice_id"],
            "as_of": pd.Timestamp(as_of_date).date(),
            "symbol": c["symbol"],
            "name": c.get("name"),
            "action": c["action"],
            "confidence": c.get("confidence"),
            "plain_summary": c.get("plain_summary"),
            "price_levels_json": json.dumps(c.get("price_levels", {}), ensure_ascii=False, default=str),
            "reasons_json": json.dumps(c.get("reasons", []), ensure_ascii=False),
            "risks_json": json.dumps(c.get("risks", []), ensure_ascii=False),
            "invalid_if_json": json.dumps(c.get("invalid_if", []), ensure_ascii=False),
            "model_run_id": model_run_id,
        })
    df = pd.DataFrame(rows)
    # key用(symbol, as_of)而不是advice_id：同一交易日内用户多次点「刷新建议」时，
    # 应该覆盖同一天的旧建议而不是无限堆积重复记录——advice_id仍然每次生成新的，
    # 只是不作为去重键，避免`advice_log`因反复刷新而膨胀出大量同symbol同日的
    # 冗余历史（2026-08-26测试「历史建议复盘」页时实测发现的问题，不是猜测）。
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
            "disclaimer": "仅供研究辅助，不构成投资建议",
        })
    return as_of, cards


def generate_daily_advice(top_n_watchlist: int = 30) -> dict:
    """一站式入口：跑一次掘金扫描 + 读当前持仓 + 生成全部建议卡片 + 落库留痕。
    供 `pages/` Streamlit页面和 `scripts/daily_pipeline.py` 复用，不重复实现。"""
    from advice.scanner import run_scan
    from common.db import get_connection, init_schema
    from risk.portfolio_optimizer import suggest_rebalance
    from risk.portfolio_risk import compute_concentration, compute_position_summary

    scan_full, scan_top, trade_date = run_scan(top_n=top_n_watchlist)
    conn = get_connection()
    init_schema(conn)
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
        model_run_id = None
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
    return {"as_of": str(trade_date), "position_cards": position_cards, "watchlist_cards": watchlist_cards}


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    result = generate_daily_advice()
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
