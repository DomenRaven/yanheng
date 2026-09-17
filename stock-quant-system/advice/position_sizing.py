"""以损定仓 + A 股 100 股取整（纯函数）。

理论：`docs/04-风险管理/10-以损定仓与A股手数.md`
费用：`research.a_share_rules.apply_costs`
"""
from __future__ import annotations

import math
from dataclasses import dataclass

from common.config import get_config
from research.a_share_rules import CostAssumptions, apply_costs


@dataclass(frozen=True)
class SizeResult:
    shares: int
    est_amount_cny: float
    size_pct_nav: float
    max_loss_cny: float
    reason: str | None = None


def sizing_params() -> dict:
    pt = get_config().get("paper_trading") or {}
    return {
        "risk_fraction": float(pt.get("risk_fraction", 0.01)),
        "max_single_weight": float(pt.get("max_single_weight", 0.15)),
        "min_cash_buffer": float(pt.get("min_cash_buffer", 0.10)),
        "max_concurrent_positions": int(pt.get("max_concurrent_positions", 3)),
        "horizon_days": int(pt.get("horizon_days", 20)),
        "default_template": str(pt.get("default_template_for_advice", "live_prep_10k")),
    }


def default_equity_cny() -> float:
    p = sizing_params()
    tpl = p["default_template"]
    templates = (get_config().get("paper_trading") or {}).get("templates") or {}
    if tpl in templates:
        return float(templates[tpl]["initial_cash_cny"])
    return 10_000.0


def _floor_lots(shares: float) -> int:
    if shares < 100:
        return 0
    return int(math.floor(shares / 100) * 100)


def compute_open_size(
    equity_cny: float,
    price: float,
    stop_price: float,
    exchange: str,
    *,
    cash_cny: float | None = None,
    cost: CostAssumptions | None = None,
) -> SizeResult:
    """买入股数：min(风险预算, 单票上限, 可用现金) 再取整到 100 股。"""
    p = sizing_params()
    if equity_cny <= 0 or price <= 0:
        return SizeResult(0, 0.0, 0.0, 0.0, "净值或价格无效")
    if stop_price >= price:
        return SizeResult(0, 0.0, 0.0, 0.0, "止损价须低于买入参考价")

    risk_cny = p["risk_fraction"] * equity_cny
    per_share_risk = price - stop_price
    by_risk = _floor_lots(risk_cny / per_share_risk)
    by_weight = _floor_lots(p["max_single_weight"] * equity_cny / price)

    usable_cash = cash_cny if cash_cny is not None else equity_cny * (1.0 - p["min_cash_buffer"])
    # 二分搜索最大可买手数（含最低佣金）
    by_cash = 0
    for lots in range(int(usable_cash // (price * 100)), 0, -1):
        sh = lots * 100
        fees = apply_costs(price * sh, "buy", exchange, cost)
        if price * sh + fees <= usable_cash + 1e-6:
            by_cash = sh
            break

    candidates = [by_risk, by_weight, by_cash]
    viable = [c for c in candidates if c >= 100]
    shares = min(viable) if viable else 0

    if shares < 100:
        return SizeResult(
            0,
            0.0,
            0.0,
            0.0,
            "买不起 1 手或一手超过风险/单票上限",
        )

    fees = apply_costs(price * shares, "buy", exchange, cost)
    amount = price * shares + fees
    max_loss = per_share_risk * shares
    return SizeResult(
        shares,
        round(amount, 2),
        round(price * shares / equity_cny, 4),
        round(max_loss, 2),
        None,
    )


def compute_sell_shares(
    held_shares: float,
    action: str,
    *,
    equity_cny: float,
    price: float,
    current_weight: float,
) -> SizeResult:
    """卖出股数：向下取整到 100；stop_loss/close 倾向清仓。"""
    held = _floor_lots(held_shares)
    if held < 100:
        return SizeResult(0, 0.0, 0.0, 0.0, "持仓不足 1 手")

    p = sizing_params()
    if action in ("stop_loss", "close"):
        target = held
    elif action == "take_profit":
        target = _floor_lots(held / 2) or 100
    elif action == "reduce":
        if current_weight <= p["max_single_weight"]:
            target = _floor_lots(held * 0.3) or 100
        else:
            excess = (current_weight - p["max_single_weight"]) * equity_cny
            target = _floor_lots(excess / price) if price > 0 else 100
            target = max(100, min(target, held))
    elif action == "rebalance":
        target = _floor_lots(held * 0.2) or 100
    else:
        return SizeResult(0, 0.0, 0.0, 0.0, "非卖出类动作")

    target = min(target, held)
    if target < 100:
        return SizeResult(0, 0.0, 0.0, 0.0, "可卖不足 1 手")
    amount = price * target
    return SizeResult(
        target,
        round(amount, 2),
        round(amount / equity_cny, 4) if equity_cny > 0 else 0.0,
        0.0,
        None,
    )


def actionable_invalid_if_open(stop_price: float | None) -> list[str]:
    return [
        "下一交易日开盘价触及涨停不可买入",
        f"收盘价低于参考止损价 {stop_price:.2f}" if stop_price else "收盘价低于参考止损价",
        "模型排名跌出前 50",
    ]


def actionable_invalid_if_position(
    action: str,
    *,
    stop_price: float | None,
    take_profit_price: float | None,
    rank_threshold: int = 50,
) -> list[str]:
    """持仓类建议的可核对失效条件（Phase 5 M8）。"""
    if action == "stop_loss":
        return [
            f"收盘价回升至参考止损价 {stop_price:.2f} 之上" if stop_price else "收盘价明显回升、浮亏收窄",
            "执行日跌停封死无法卖出（需改日再卖）",
        ]
    if action == "reduce":
        return [
            f"单票占净值回落到 15% 以下" if stop_price is None else "单票占净值已回到上限以内",
            f"模型排名重新进入前 {rank_threshold} 且集中度不再超标",
        ]
    if action == "take_profit":
        return [
            f"收盘价回落至参考止盈价 {take_profit_price:.2f} 之下" if take_profit_price else "浮盈明显回吐",
            f"模型排名重新进入前 {rank_threshold}",
        ]
    if action == "rebalance":
        return ["当前权重与优化目标偏差已小于 3%", "执行日不可交易（停牌或涨跌停）"]
    return []
