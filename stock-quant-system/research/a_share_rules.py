"""A股可成交性与交易成本规则。

数字来源：理论库 `docs/01-基础概念/08-A股交易制度深化.md`（2026-08 复核）
与 `docs/01-基础概念/02-A股市场制度.md`。涨跌幅做成日期敏感参数，避免
把 2026-07-06 前后的 ST 规则混用。新股上市前 5 日不设涨跌幅暂未实现
（需要上市日日历），回测时对上市不足 5 日的样本应直接过滤，见
`is_too_new()`。
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass

ST_LIMIT_CHANGE_DATE = dt.date(2026, 7, 6)


@dataclass(frozen=True)
class CostAssumptions:
    commission_rate: float = 0.0003  # 买卖各收，券商折扣佣金的常见量级
    min_commission: float = 5.0
    stamp_tax_rate: float = 0.0005  # 卖出单边，2023-08-28 起万分之五
    transfer_fee_rate: float = 0.00001  # 过户费约成交额的 0.01‰
    slippage_rate: float = 0.001  # 单向滑点假设，可在回测里单独敏感性分析


def limit_pct(board: str, is_st: bool, trade_date: dt.date) -> float:
    """返回当日涨跌幅限制（小数，如 0.10）。新股前5日请调用方自行过滤。"""
    if board == "bse":
        return 0.30
    if board in ("gem", "star"):
        return 0.20
    if is_st and trade_date < ST_LIMIT_CHANGE_DATE:
        return 0.05
    return 0.10


def is_limit_locked(
    prev_close: float,
    close: float,
    board: str,
    is_st: bool,
    trade_date: dt.date,
    *,
    tolerance: float = 0.002,
) -> tuple[bool, bool]:
    """判断当日是否涨停/跌停封死。返回 (is_up_limit, is_down_limit)。

    用收盘价相对前收盘是否触及阈值近似；真正的"一字板无法买入"还需要
    成交量接近 0 或涨跌停价停留，这里先给阈值判定，Phase 1 回测引擎再叠加量能。
    """
    if prev_close is None or prev_close <= 0 or close is None:
        return False, False
    cap = limit_pct(board, is_st, trade_date)
    up = prev_close * (1 + cap)
    down = prev_close * (1 - cap)
    return close >= up * (1 - tolerance), close <= down * (1 + tolerance)


DELISTING_PERIOD_MIN_ASSET_CNY = 500_000.0  # 上交所交易规则 4.4.5 / 深交所交易规则 4.5.3（2026 年修订）


def is_delisting_period(name: str | None) -> bool:
    """退市整理期标的：深市简称后缀“退”，沪市简称前缀“退市”（两所风险揭示书原文）。

    个人投资者买入退市整理股票须满足 2 年交易经历且前 20 个交易日日均资产
    不低于 50 万元（上交所 4.4.5、深交所 4.5.3，2026 年修订版），并签署风险揭示书。
    万元级账户在券商端会被直接拒单，因此这类标的对本项目的用户不可买入。
    """
    if not name:
        return False
    text = str(name).strip()
    return text.startswith("退市") or text.endswith("退")


def retail_can_buy_delisting_period(account_asset_cny: float, years_trading: float = 0.0) -> bool:
    """万元级账户的默认答案是 False；满足 50 万元 + 2 年时返回 True（仍需签风险揭示书）。"""
    return account_asset_cny >= DELISTING_PERIOD_MIN_ASSET_CNY and years_trading >= 2.0


def is_too_new(list_date: dt.date | None, as_of: dt.date, min_listed_days: int = 60) -> bool:
    """次新过滤：默认上市不满 60 个自然日（约 2 个月），可配置。"""
    if list_date is None:
        return False
    return (as_of - list_date).days < min_listed_days


def round_trip_cost_rate(
    exchange: str,
    assumptions: CostAssumptions | None = None,
) -> float:
    """买入+卖出一轮的费率（不含滑点）。北交所暂免印花税。"""
    a = assumptions or CostAssumptions()
    stamp = 0.0 if exchange == "bj" else a.stamp_tax_rate
    return 2 * a.commission_rate + 2 * a.transfer_fee_rate + stamp


def apply_costs(
    notional: float,
    side: str,
    exchange: str,
    assumptions: CostAssumptions | None = None,
) -> float:
    """返回该笔交易应扣费用（佣金+印花税+过户费，不含滑点）。side: buy/sell。"""
    a = assumptions or CostAssumptions()
    commission = max(abs(notional) * a.commission_rate, a.min_commission)
    transfer = abs(notional) * a.transfer_fee_rate
    stamp = abs(notional) * a.stamp_tax_rate if (side == "sell" and exchange != "bj") else 0.0
    return commission + transfer + stamp
