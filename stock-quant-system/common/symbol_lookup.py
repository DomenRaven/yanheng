"""按公司名称或代码查找股票——只读 `universe` 表，不新建数据源。

名称来自股票池同步（`ingestion/universe.py`），与扫描/持仓用的是同一份名单。
"""
from __future__ import annotations

import re

import pandas as pd
import streamlit as st


_EMPTY_COLS = ["symbol", "name", "exchange", "board", "is_st", "is_delisted"]
_CODE_RE = re.compile(r"^\d{1,6}$")


def lookup_universe(conn, query: str, limit: int = 30) -> pd.DataFrame:
    """返回匹配行。纯查询，不碰 UI。空查询返回空表。"""
    q = (query or "").strip().replace(" ", "")
    q = q.replace("%", "").replace("_", "")
    if not q:
        return pd.DataFrame(columns=_EMPTY_COLS)

    if _CODE_RE.fullmatch(q):
        code = q.zfill(6)
        exact = conn.execute(
            """
            SELECT symbol, name, exchange, board, is_st, is_delisted
            FROM universe WHERE symbol = ?
            """,
            [code],
        ).df()
        if not exact.empty:
            return exact
        # 输入 600 这种前缀，走模糊
        return conn.execute(
            """
            SELECT symbol, name, exchange, board, is_st, is_delisted
            FROM universe
            WHERE symbol LIKE ?
            ORDER BY is_delisted ASC, symbol
            LIMIT ?
            """,
            [f"{q}%", limit],
        ).df()

    like = f"%{q}%"
    prefix = f"{q}%"
    return conn.execute(
        """
        SELECT symbol, name, exchange, board, is_st, is_delisted
        FROM universe
        WHERE name ILIKE ? OR symbol LIKE ?
        ORDER BY
            CASE
                WHEN name = ? THEN 0
                WHEN name ILIKE ? THEN 1
                ELSE 2
            END,
            is_delisted ASC,
            length(name) ASC,
            symbol
        LIMIT ?
        """,
        [like, f"{q}%", q, prefix, limit],
    ).df()


def render_symbol_picker(
    conn,
    *,
    key: str,
    label: str = "股票代码或公司名称",
) -> str | None:
    """输入「紫金矿业」或「601899」，返回 6 位代码；多匹配时用下拉选择。"""
    query = st.text_input(
        label,
        key=key,
        placeholder="例如 601899 或 紫金矿业",
        help="本地股票池模糊匹配，支持全称、简称片段、6 位代码。退市/ST 会在选项里标明。",
    )
    hits = lookup_universe(conn, query)
    if not (query or "").strip():
        return None
    if hits.empty:
        st.warning(f"股票池里找不到「{query.strip()}」。可换更短关键词，或等股票池更新完成后再试。")
        return None

    labels: list[str] = []
    symbols: list[str] = []
    for _, r in hits.iterrows():
        tags = []
        if bool(r.get("is_st")):
            tags.append("ST")
        if bool(r.get("is_delisted")):
            tags.append("退市")
        tag = f"  [{' '.join(tags)}]" if tags else ""
        labels.append(f"{r['symbol']}  {r['name']}  {r['exchange']}{tag}")
        symbols.append(str(r["symbol"]))

    if len(symbols) == 1:
        st.caption(f"已匹配：{labels[0]}")
        return symbols[0]

    chosen = st.selectbox(
        f"匹配到 {len(symbols)} 只，请选择",
        options=list(range(len(symbols))),
        format_func=lambda i: labels[i],
        key=f"{key}_choice",
    )
    return symbols[int(chosen)]
