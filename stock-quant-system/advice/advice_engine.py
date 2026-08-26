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


def build_advice_for_watchlist(scan_top: pd.DataFrame) -> list[dict]:
    """未持有、但在掘金Top候选里的股票 -> watch/open建议。scan_top 来自
    `advice/scanner.py::run_scan()` 的 top_pick 返回值（已含behavior信号列）。"""
    cards = []
    for _, row in scan_top.iterrows():
        if not row.get("is_tradable", True):
            action, confidence = "watch", 0.0
            risks = ["当前不可交易（停牌或涨停封死）"]
        elif row["rank"] <= _OPEN_CANDIDATE_RANK_THRESHOLD:
            action = "open"
            confidence = round(max(0.0, 1.0 - row["rank"] / _OPEN_CANDIDATE_RANK_THRESHOLD), 3)
            risks = _risks_from_behavior(row)
        else:
            action, confidence = "watch", 0.0
            risks = _risks_from_behavior(row)

        cards.append({
            "advice_id": str(uuid.uuid4()),
            "symbol": row["symbol"],
            "name": row.get("name"),
            "action": action,
            "confidence": confidence,
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

        cards.append({
            "advice_id": str(uuid.uuid4()),
            "symbol": symbol,
            "action": action,
            "confidence": confidence,
            "size_pct_nav": [0.0, round(weight, 4)],
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
            "action": c["action"],
            "confidence": c.get("confidence"),
            "reasons_json": json.dumps(c.get("reasons", []), ensure_ascii=False),
            "risks_json": json.dumps(c.get("risks", []), ensure_ascii=False),
            "invalid_if_json": json.dumps(c.get("invalid_if", []), ensure_ascii=False),
            "model_run_id": model_run_id,
        })
    df = pd.DataFrame(rows)
    upsert(conn, "advice_log", ["advice_id"], df)


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
