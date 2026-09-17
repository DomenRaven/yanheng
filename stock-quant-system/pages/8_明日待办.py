"""明日待办：从最新建议卡片提炼最多 3 条次日可执行项（Phase 5 阶段 C）。"""
from __future__ import annotations

import datetime as dt

import streamlit as st

from advice.advice_engine import load_latest_advice_cards
from advice.entry_rules import build_tomorrow_todos
from common.ui_theme import action_meta, apply_theme, connect_warehouse, render_trust_footer, section_header

apply_theme(page_title="明日待办", page_icon="📋")
st.title("📋 明日待办")
st.caption(
    "在「持仓与建议」生成卡片后，这里列出下一交易日最多 3 条优先动作。"
    " 开盘涨停或停牌会自动标记为暂不可执行。"
)

conn = connect_warehouse()
try:
    as_of, cards = load_latest_advice_cards(conn)
    if not as_of or not cards:
        st.info("尚无建议记录。请先在「持仓与建议」页点击「生成/刷新建议卡片」。")
    else:
        signal_d = dt.date.fromisoformat(as_of) if isinstance(as_of, str) else as_of
        todos = build_tomorrow_todos(cards, signal_d, conn, max_items=3)
        st.caption(f"信号日 {as_of} → 待办条数 {len(todos)}")
        if not todos:
            st.success("当前没有需要次日优先执行的动作（或 open 建议未通过以损定仓）。")
        for item in todos:
            meta = action_meta(item["action"])
            with st.container(border=True):
                st.markdown(
                    f"<span style='background:{meta['bg']};color:{meta['color']};"
                    f"padding:4px 10px;border-radius:8px;font-weight:700;'>"
                    f"{meta['emoji']} {meta['label']}</span> "
                    f"**{item['symbol']}** {item.get('name') or ''}",
                    unsafe_allow_html=True,
                )
                st.write(f"执行日：**{item['exec_date']}**")
                if item.get("size_shares"):
                    st.write(f"建议股数：**{int(item['size_shares'])}** 股")
                if item.get("est_amount_cny"):
                    st.write(f"约需资金：¥{float(item['est_amount_cny']):,.0f}")
                if item.get("blocked"):
                    st.warning(f"暂不可按规则执行：{item.get('block_reason') or '—'}")
                if item.get("plain_summary"):
                    st.caption(item["plain_summary"])
        section_header("说明", level=2)
        st.markdown(
            "- 待办来自同一套 `entry_rules` 纯函数，与回测脚本共用逻辑。\n"
            "- 不等于自动下单；模拟成交请在「持仓与建议 → 本机模拟盘」操作。"
        )
finally:
    render_trust_footer(conn)
    conn.close()
