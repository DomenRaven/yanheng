"""
羊群效应（Herding）/ 拥挤度代理指标。

理论依据：`docs/02-经典理论/06-市场博弈与信息不对称.md`、`docs/09-场景决策案例/`。
羊群效应体现为"大家都在买同一批股票/同一个方向"，用三个可从已有数据算出的代理拼出来：

1. **动量拥挤度**（市场层面，全部候选股共享一个数）：截面上 `factor_mom_12_1 > 0`
   的股票占比——占比越极端（接近0或1），说明市场情绪一致性越高，趋势反转风险也越高
   （对齐 `research/factors.py` 已有的动量因子，不重复定义）。
2. **龙虎榜频次**（个股层面）：过去N个交易日该股票上龙虎榜的次数——频繁上榜是资金
   高度关注/游资博弈的直接证据（`dragon_tiger_list` 已有真实数据，2026-08-25起Tushare
   补齐，184174条记录）。
3. **大单资金流方向一致性**（个股层面）：过去N日大单+超大单净买入为正的交易日占比
   ——持续同向说明主力资金在同一方向"抱团"，也是羊群效应的直接体现（`moneyflow` 表，
   1088万条真实记录）。
"""
from __future__ import annotations

import logging

import pandas as pd

logger = logging.getLogger("behavior.crowding")

_LOOKBACK_DEFAULT = 20


def compute_market_momentum_crowding(conn, as_of_date: str) -> float:
    """全市场层面：截面上 factor_mom_12_1(动量) > 0 的股票占比。用 daily_quotes 现算
    12月收益(剔近1月)方向，不依赖 research/panel.py 的月度面板（那是月度截面，扫描器
    是逐日运行，这里现算避免引入面板依赖）。"""
    row = conn.execute(
        """
        WITH px AS (
            SELECT symbol, trade_date, close,
                   lag(close, 21) OVER (PARTITION BY symbol ORDER BY trade_date) AS close_1m_ago,
                   lag(close, 252) OVER (PARTITION BY symbol ORDER BY trade_date) AS close_12m_ago
            FROM daily_quotes
            WHERE adjust = 'qfq' AND trade_date <= ?
        ),
        latest AS (
            SELECT symbol, close, close_1m_ago, close_12m_ago
            FROM px
            WHERE trade_date = ?
        )
        SELECT
            avg(CASE WHEN (close_1m_ago / close_12m_ago - 1) > 0 THEN 1.0 ELSE 0.0 END) AS pct_positive,
            count(*) AS n
        FROM latest
        WHERE close_1m_ago IS NOT NULL AND close_12m_ago IS NOT NULL AND close_12m_ago > 0
        """,
        [as_of_date, as_of_date],
    ).fetchone()
    return float(row[0]) if row and row[0] is not None else float("nan")


def compute_dragon_tiger_frequency(conn, symbols: list[str], as_of_date: str, lookback: int = _LOOKBACK_DEFAULT) -> pd.DataFrame:
    if not symbols:
        return pd.DataFrame(columns=["symbol", "dt_count"])
    placeholders = ",".join(["?"] * len(symbols))
    df = conn.execute(
        f"""
        SELECT symbol, count(distinct trade_date) AS dt_count
        FROM dragon_tiger_list
        WHERE symbol IN ({placeholders})
          AND trade_date <= ?
          AND trade_date > (CAST(? AS DATE) - INTERVAL '{lookback * 2} days')
        GROUP BY symbol
        """,
        symbols + [as_of_date, as_of_date],
    ).df()
    return df


def compute_moneyflow_herding(conn, symbols: list[str], as_of_date: str, lookback: int = _LOOKBACK_DEFAULT) -> pd.DataFrame:
    """过去N日大单+超大单净买入(buy-sell)为正的交易日占比，越接近1/0说明资金方向越一致。"""
    if not symbols:
        return pd.DataFrame(columns=["symbol", "big_order_consistency"])
    placeholders = ",".join(["?"] * len(symbols))
    df = conn.execute(
        f"""
        WITH ranked AS (
            SELECT symbol, trade_date,
                   (buy_lg_amount + buy_elg_amount) - (sell_lg_amount + sell_elg_amount) AS net_big,
                   row_number() OVER (PARTITION BY symbol ORDER BY trade_date DESC) AS rn
            FROM moneyflow
            WHERE symbol IN ({placeholders}) AND trade_date <= ?
        )
        SELECT symbol,
               avg(CASE WHEN net_big > 0 THEN 1.0 ELSE 0.0 END) AS pct_net_buy_days,
               count(*) AS n
        FROM ranked
        WHERE rn <= {lookback}
        GROUP BY symbol
        """,
        symbols + [as_of_date],
    ).df()
    if df.empty:
        return df
    # 一致性 = 距离0.5的偏离度*2，落在[0,1]，1表示N天全部同一方向（最拥挤）
    df["big_order_consistency"] = (df["pct_net_buy_days"] - 0.5).abs() * 2
    return df[["symbol", "pct_net_buy_days", "big_order_consistency"]]


def compute_crowding_signals(conn, symbols: list[str], as_of_date: str, lookback: int = _LOOKBACK_DEFAULT) -> pd.DataFrame:
    """拼出个股层面的拥挤度汇总表，附带市场层面的动量拥挤度（同一个数广播到每一行）。"""
    market_crowding = compute_market_momentum_crowding(conn, as_of_date)
    dt_freq = compute_dragon_tiger_frequency(conn, symbols, as_of_date, lookback)
    mf_herd = compute_moneyflow_herding(conn, symbols, as_of_date, lookback)

    out = pd.DataFrame({"symbol": symbols})
    out = out.merge(dt_freq, on="symbol", how="left").merge(mf_herd, on="symbol", how="left")
    out["dt_count"] = out["dt_count"].fillna(0).astype(int)
    out["market_momentum_crowding"] = market_crowding

    def _flag(row):
        parts = []
        if row["dt_count"] >= 3:
            parts.append(f"近{lookback*2}日上榜龙虎榜{row['dt_count']}次(游资高度关注)")
        if pd.notna(row.get("big_order_consistency")) and row["big_order_consistency"] > 0.6:
            direction = "净买入" if row.get("pct_net_buy_days", 0) > 0.5 else "净卖出"
            parts.append(f"大单{lookback}日内{direction}方向高度一致(拥挤度{row['big_order_consistency']:.2f})")
        return "；".join(parts) if parts else "无明显拥挤信号"

    out["crowding_flag"] = out.apply(_flag, axis=1)
    return out


if __name__ == "__main__":
    import logging as _logging

    from common.db import get_connection

    _logging.basicConfig(level=_logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    conn = get_connection()
    syms = conn.execute(
        "SELECT symbol FROM universe WHERE is_delisted=FALSE AND is_st=FALSE LIMIT 20"
    ).df()["symbol"].tolist()
    latest = conn.execute("SELECT max(trade_date) FROM daily_quotes WHERE adjust='qfq'").fetchone()[0]
    result = compute_crowding_signals(conn, syms, str(latest))
    conn.close()
    print(result.to_string(index=False))
