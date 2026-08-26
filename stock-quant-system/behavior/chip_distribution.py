"""
处置效应（Disposition Effect）代理指标。

理论依据：`docs/02-经典理论/05-交易心理学专论.md`、`docs/08-量价与技术指标/06-筹码分布与
成本理论.md`，以及 Grinblatt & Han (2005) 的"参考价格"模型——投资者以近期持仓成本为心理
参考点，现价高于参考成本时更容易兑现浮盈（抛压增加/上涨乏力），现价低于参考成本时惜售
（下跌缩量/反弹乏力）。

**数据局限（如实说明，对齐vibe coding八荣八耻第7条）**：Tushare 官方逐日筹码分布/胜率接口
（`cyq_perf`/`cyq_chips`）在当前账户2000积分档没有访问权限（2026-08-26已用真实请求验证，
返回`[40203] 权限不足`），本模块不是"官方真实筹码分布"，而是用已有的量价数据构造一个
学术上可追溯的替代代理：

    reference_cost ≈ 过去N个交易日成交额加权平均价（VWAP，用每日成交额/成交量近似当日成交
    均价，再按成交量加权），把每日成交量当作"当天新增的筹码"，只要没有大规模换手清仓，
    多数持仓成本会落在近期VWAP附近——这是处置效应文献里常用的简化参考价格构造法之一，
    不是对全部历史逐笔成交的精确复原。

winner_rate的粗代理：过去N日里，当日VWAP低于当前收盘价的交易日成交量占比（近似"这部分筹码
现在是浮盈的"），不是真实持仓分布的获利盘比例。
"""
from __future__ import annotations

import logging

import numpy as np
import pandas as pd

logger = logging.getLogger("behavior.chip_distribution")

_LOOKBACK_DEFAULT = 60


def compute_disposition_proxy(
    conn,
    symbols: list[str],
    as_of_date: str,
    lookback: int = _LOOKBACK_DEFAULT,
) -> pd.DataFrame:
    """给定股票列表和截面日，算处置效应代理指标。

    返回列：symbol, ref_cost, close, unrealized_gain_pct, winner_rate_proxy,
    disposition_flag（文本标签，供 scanner 展示）。
    """
    if not symbols:
        return pd.DataFrame(
            columns=["symbol", "ref_cost", "close", "unrealized_gain_pct", "winner_rate_proxy", "disposition_flag"]
        )
    placeholders = ",".join(["?"] * len(symbols))
    daily = conn.execute(
        f"""
        SELECT symbol, trade_date, close, volume, amount
        FROM daily_quotes
        WHERE adjust = 'qfq' AND symbol IN ({placeholders}) AND trade_date <= ?
        ORDER BY symbol, trade_date
        """,
        symbols + [as_of_date],
    ).df()
    if daily.empty:
        return pd.DataFrame(
            columns=["symbol", "ref_cost", "close", "unrealized_gain_pct", "winner_rate_proxy", "disposition_flag"]
        )

    rows = []
    for symbol, grp in daily.groupby("symbol", sort=False):
        grp = grp.tail(lookback).copy()
        if len(grp) < max(10, lookback // 3):
            continue
        # 当日均价用成交额/成交量近似（volume单位为"手"，amount单位为"元"，比例不受单位影响）
        grp["vwap_day"] = grp["amount"] / grp["volume"].replace(0, np.nan)
        grp["vwap_day"] = grp["vwap_day"].fillna(grp["close"])
        vol_sum = grp["volume"].sum()
        if not vol_sum or vol_sum <= 0:
            continue
        ref_cost = float((grp["vwap_day"] * grp["volume"]).sum() / vol_sum)
        close = float(grp["close"].iloc[-1])
        gain_pct = close / ref_cost - 1 if ref_cost else np.nan
        winner_vol = grp.loc[grp["vwap_day"] < close, "volume"].sum()
        winner_rate_proxy = float(winner_vol / vol_sum)

        if gain_pct is not None and pd.notna(gain_pct):
            if gain_pct > 0.30:
                flag = "浮盈筹码集中(处置效应下警惕获利兑现带来的上涨乏力)"
            elif gain_pct < -0.20:
                flag = "浮亏筹码集中(处置效应下惜售，下跌可能缩量但反弹也乏力)"
            else:
                flag = "筹码成本与现价接近，处置效应影响不明显"
        else:
            flag = "数据不足"

        rows.append(
            {
                "symbol": symbol,
                "ref_cost": round(ref_cost, 4) if ref_cost else None,
                "close": close,
                "unrealized_gain_pct": round(gain_pct, 4) if pd.notna(gain_pct) else None,
                "winner_rate_proxy": round(winner_rate_proxy, 4),
                "disposition_flag": flag,
            }
        )
    return pd.DataFrame(rows)


if __name__ == "__main__":
    import logging as _logging

    from common.db import get_connection

    _logging.basicConfig(level=_logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    conn = get_connection()
    syms = conn.execute(
        "SELECT symbol FROM universe WHERE is_delisted=FALSE AND is_st=FALSE LIMIT 20"
    ).df()["symbol"].tolist()
    latest = conn.execute("SELECT max(trade_date) FROM daily_quotes WHERE adjust='qfq'").fetchone()[0]
    result = compute_disposition_proxy(conn, syms, str(latest))
    conn.close()
    print(result.to_string(index=False))
