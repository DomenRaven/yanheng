"""小账户行业暴露上限（规格 S2）。

理论：
- `docs/03-量化方法/01-多因子与Barra.md`：行业是共同因子；不想要的暴露应约束。
- `docs/03-量化方法/04-组合优化.md`：产品默认约束含行业偏离；散户多头现金账户。
- `docs/04-风险管理/02-仓位与资金管理.md`：单行业上限；相关持仓合并计量。
- `docs/01-基础概念/05-产业面与行业分析.md`：同行业多只 ≠ 分散。
- 规格 C3：完整机构中性是 Could。本模块用离散规则「开仓待办同一 industry_code 最多 1 只」。

止损/减仓不受本规则拦截（生存风控优先于结构）。
"""
from __future__ import annotations

import datetime as dt
from typing import Iterable

import pandas as pd


def latest_industry_codes(
    conn,
    symbols: Iterable[str],
    as_of: dt.date | str,
) -> dict[str, str]:
    """申万分类 ASOF：每个 symbol 取 start_date <= as_of 的最新一条。"""
    syms = [str(s) for s in symbols if s]
    if not syms:
        return {}
    as_of_d = pd.Timestamp(as_of).date()
    placeholders = ",".join(["?"] * len(syms))
    df = conn.execute(
        f"""
        SELECT symbol, industry_code FROM (
            SELECT symbol, industry_code, start_date,
                   ROW_NUMBER() OVER (
                       PARTITION BY symbol ORDER BY start_date DESC
                   ) AS rn
            FROM industry_classification
            WHERE symbol IN ({placeholders})
              AND start_date <= ?
        ) t
        WHERE rn = 1
        """,
        [*syms, as_of_d],
    ).df()
    if df.empty:
        return {}
    return {
        str(r["symbol"]): str(r["industry_code"])
        for _, r in df.iterrows()
        if pd.notna(r.get("industry_code")) and str(r["industry_code"]).strip()
    }


def attach_industry(conn, df: pd.DataFrame, as_of: dt.date | str) -> pd.DataFrame:
    if df.empty or "symbol" not in df.columns:
        return df
    out = df.copy()
    mapping = latest_industry_codes(conn, out["symbol"].tolist(), as_of)
    out["industry_code"] = out["symbol"].map(mapping)
    return out


def cap_opens_one_per_industry(
    items: list[dict],
    industry_by_symbol: dict[str, str],
    *,
    max_items: int,
) -> list[dict]:
    """按已排序待办依次收取；open 遇已占用行业则跳过，给后续名额。

    无行业代码的 open 不占行业名额（数据缺口，不假装中性）。
    """
    used: set[str] = set()
    picked: list[dict] = []
    for raw in items:
        item = dict(raw)
        symbol = str(item.get("symbol") or "")
        ind = industry_by_symbol.get(symbol)
        if ind:
            item["industry_code"] = ind
        if item.get("action") == "open" and ind:
            if ind in used:
                continue
            used.add(ind)
        picked.append(item)
        if len(picked) >= max_items:
            break
    return picked
