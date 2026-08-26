"""
情绪代理指标（市场热度 + 大单小单背离）。

**范围说明（如实标注，对齐vibe coding八荣八耻第2/7条）**：文件名沿用计划文档
`个人炒股辅助本地应用_76077b1a.plan.md` 第5节目录蓝图里的 `sentiment_nlp.py`，但目前
**不做真正的新闻文本NLP情绪打分**——原因：(1) 真正的舆情NLP需要先把新闻/研报文本
批量抓取入库并做摘要/情绪分类，这正是计划文档 Phase 4 `llm/explain_assistant.py`
（联网资讯 + LLM解释层）要做的事，此处提前重复实现会违反"分层不越界"的项目规范；
(2) 全市场逐股票做文本情绪打分的计算量/API成本在没有先跑通Phase 4资讯管道前无法评估。
本模块先用**已有真实数据**算两个可解释、可验证的情绪代理，Phase 4接入真实新闻文本后
可以直接把 LLM 情绪分数补充为第三个信号，不需要改这里的接口。

1. **市场热度**（全市场层面）：当日涨停/跌停家数比例——用 `limit_price` + `daily_quotes`
   现算，涨停家数占比越高说明市场情绪越亢奋（对应"过热"警示），跌停家数占比越高说明
   恐慌情绪越重。
2. **大单小单背离**（个股层面，对应用户明确提到的"大单买入同时小单卖出"可疑盘口场景）：
   `moneyflow` 表里大单+超大单净买入方向 与 小单净买入方向 是否相反——方向相反且金额
   都不小，是主力吸筹/派发中散户跟风方向错误的经典信号。
"""
from __future__ import annotations

import logging

import pandas as pd

logger = logging.getLogger("behavior.sentiment_nlp")

_LOOKBACK_DEFAULT = 5


def compute_market_heat(conn, as_of_date: str) -> dict:
    """全市场当日涨停/跌停家数比例（用 limit_price 官方涨跌停价 + qfq收盘价判断封板）。"""
    row = conn.execute(
        """
        SELECT
            count(*) AS n_total,
            sum(CASE WHEN dq.close >= lp.up_limit * 0.998 THEN 1 ELSE 0 END) AS n_limit_up,
            sum(CASE WHEN dq.close <= lp.down_limit * 1.002 THEN 1 ELSE 0 END) AS n_limit_down
        FROM limit_price lp
        JOIN daily_quotes dq
            ON dq.symbol = lp.symbol AND dq.trade_date = lp.trade_date AND dq.adjust = 'qfq'
        WHERE lp.trade_date = ?
        """,
        [as_of_date],
    ).fetchone()
    n_total, n_up, n_down = row
    if not n_total:
        return {"n_total": 0, "n_limit_up": 0, "n_limit_down": 0, "limit_up_ratio": None, "heat_flag": "无数据"}
    up_ratio = n_up / n_total
    down_ratio = n_down / n_total
    if up_ratio > 0.05:
        flag = f"涨停家数占比{up_ratio:.1%}，市场情绪偏亢奋"
    elif down_ratio > 0.03:
        flag = f"跌停家数占比{down_ratio:.1%}，市场情绪偏恐慌"
    else:
        flag = "市场涨跌停家数比例正常，情绪中性"
    return {
        "n_total": int(n_total),
        "n_limit_up": int(n_up),
        "n_limit_down": int(n_down),
        "limit_up_ratio": round(up_ratio, 4),
        "limit_down_ratio": round(down_ratio, 4),
        "heat_flag": flag,
    }


def compute_big_small_divergence(conn, symbols: list[str], as_of_date: str, lookback: int = _LOOKBACK_DEFAULT) -> pd.DataFrame:
    """过去N日大单+超大单净买入 与 小单净买入 方向是否相反（"大买小卖"/"大卖小买"）。"""
    if not symbols:
        return pd.DataFrame(columns=["symbol", "divergence_flag"])
    placeholders = ",".join(["?"] * len(symbols))
    df = conn.execute(
        f"""
        WITH ranked AS (
            SELECT symbol, trade_date,
                   (buy_lg_amount + buy_elg_amount - sell_lg_amount - sell_elg_amount) AS net_big,
                   (buy_sm_amount - sell_sm_amount) AS net_small,
                   row_number() OVER (PARTITION BY symbol ORDER BY trade_date DESC) AS rn
            FROM moneyflow
            WHERE symbol IN ({placeholders}) AND trade_date <= ?
        )
        SELECT symbol, sum(net_big) AS net_big_sum, sum(net_small) AS net_small_sum
        FROM ranked
        WHERE rn <= {lookback}
        GROUP BY symbol
        """,
        symbols + [as_of_date],
    ).df()
    if df.empty:
        return df

    def _flag(row):
        # moneyflow 的 *_amount 字段单位是 Tushare 官方文档定义的"万元"，这里直接展示，不再除以1e4
        big, small = row["net_big_sum"], row["net_small_sum"]
        if pd.isna(big) or pd.isna(small):
            return "数据不足"
        if big > 0 and small < 0:
            return f"大单买入小单卖出(近{lookback}日大单净流入{big:.0f}万元，小单净流出{-small:.0f}万元)"
        if big < 0 and small > 0:
            return f"大单卖出小单买入(近{lookback}日大单净流出{-big:.0f}万元，小单净流入{small:.0f}万元)"
        return "大小单方向一致，无背离信号"

    df["divergence_flag"] = df.apply(_flag, axis=1)
    return df[["symbol", "net_big_sum", "net_small_sum", "divergence_flag"]]


if __name__ == "__main__":
    import logging as _logging

    from common.db import get_connection

    _logging.basicConfig(level=_logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    conn = get_connection()
    latest = conn.execute("SELECT max(trade_date) FROM daily_quotes WHERE adjust='qfq'").fetchone()[0]
    heat = compute_market_heat(conn, str(latest))
    print("市场热度:", heat)
    syms = conn.execute(
        "SELECT symbol FROM universe WHERE is_delisted=FALSE AND is_st=FALSE LIMIT 20"
    ).df()["symbol"].tolist()
    div = compute_big_small_divergence(conn, syms, str(latest))
    conn.close()
    print(div.to_string(index=False))
