"""明日待办：从最新建议卡片提炼最多 3 条次日可执行项（Phase 5 阶段 C）。"""
from __future__ import annotations

import streamlit as st

from advice.advice_engine import load_latest_advice_cards, parse_as_of_date
from advice.entry_rules import build_tomorrow_todos
from common.ui_theme import (
    action_meta,
    apply_theme,
    close_warehouse,
    connect_warehouse,
    require_warehouse_for_decisions,
    section_header,
)

apply_theme(page_title="明日待办", page_icon="📋")
st.title("📋 明日待办")
st.caption(
    "在「持仓与建议」生成卡片后，这里列出下一交易日最多 3 条优先动作。"
    "开盘涨停或停牌会标为暂不可执行。练模拟成交请回「持仓与建议 → ② 本机模拟盘」。"
)

conn = connect_warehouse()
try:
    require_warehouse_for_decisions(conn)
    todo_pool = st.radio(
        "待办池",
        ["hs", "bj"],
        format_func=lambda x: "沪深池" if x == "hs" else "北交所池",
        horizontal=True,
        key="todo_pool",
    )
    as_of, cards = load_latest_advice_cards(conn, pool_id=todo_pool)
    if not as_of or not cards:
        st.info(
            f"「{'沪深' if todo_pool == 'hs' else '北交所'}池」尚无建议记录。"
            "请先在「持仓与建议」切换同池并点击「生成/刷新建议卡片」。"
        )
    else:
        signal_d = parse_as_of_date(as_of)
        allow_bj = todo_pool == "bj" or bool(
            st.session_state.get("allow_bj_open_in_todos")
        )
        todos = build_tomorrow_todos(
            cards, signal_d, conn, max_items=3, allow_bj_open=allow_bj
        )
        st.caption(
            f"池：{'沪深' if todo_pool == 'hs' else '北交所'} · "
            f"信号日 {signal_d.isoformat()} → 待办条数 {len(todos)}"
        )
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
                if item.get("industry_code"):
                    st.caption(f"行业代码 {item['industry_code']}（开仓待办：同一行业最多 1 只）")
        section_header("说明", level=2)
        st.markdown(
            "- 待办与回测脚本共用同一套入场规则。\n"
            "- 开仓候选同一行业最多 1 只；止损与减仓不受此条限制。\n"
            "- 本页不会自动下单；模拟成交请到「持仓与建议 → 本机模拟盘」操作。"
        )
finally:
    close_warehouse(conn)
