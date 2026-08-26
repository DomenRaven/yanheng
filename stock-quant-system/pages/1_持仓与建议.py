"""持仓管理（手动录入，不接券商API） + 该持仓对应的建议卡片。"""
from __future__ import annotations

import datetime as dt
import uuid

import pandas as pd
import streamlit as st

from common.db import get_connection, init_schema
from common.ui_theme import apply_theme, render_advice_card

apply_theme(page_title="持仓与建议", page_icon="💼")
st.title("💼 持仓与建议")

conn = get_connection()
init_schema(conn)

st.subheader("录入新持仓")
with st.form("add_position_form", clear_on_submit=True):
    c1, c2, c3, c4 = st.columns(4)
    symbol = c1.text_input("股票代码（不带交易所后缀，如 000001）")
    shares = c2.number_input("股数", min_value=0.0, step=100.0)
    cost_price = c3.number_input("建仓成本价（每股）", min_value=0.0, step=0.01, format="%.2f")
    opened_at = c4.date_input("建仓日期", value=dt.date.today())
    note = st.text_input("备注（可选）")
    submitted = st.form_submit_button("添加持仓批次", type="primary")
    if submitted:
        if not symbol or shares <= 0 or cost_price <= 0:
            st.error("股票代码、股数、成本价均为必填且需大于0")
        else:
            conn.execute(
                "INSERT INTO positions (lot_id, symbol, shares, cost_price, opened_at, note) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                [str(uuid.uuid4()), symbol.strip(), shares, cost_price, opened_at, note or None],
            )
            st.success(f"已添加持仓：{symbol} {shares}股 @ {cost_price}")
            st.rerun()

st.divider()
st.subheader("当前持仓（未平仓批次）")

from risk.portfolio_risk import compute_position_summary  # noqa: E402

today = dt.date.today().strftime("%Y-%m-%d")
position_summary = compute_position_summary(conn, today)

if position_summary.empty:
    st.info("暂无持仓记录。在上方表单录入你的第一笔持仓吧。")
else:
    for _, row in position_summary.iterrows():
        with st.container(border=True):
            cols = st.columns([2, 1, 1, 1, 1, 1])
            cols[0].markdown(f"**{row['symbol']}** (批次 {row['lot_id'][:8]})")
            cols[1].write(f"{row['shares']:.0f} 股")
            cols[2].write(f"成本 ¥{row['cost_price']:.2f}")
            has_price = pd.notna(row["last_close"])
            cols[3].write(f"最新 ¥{row['last_close']:.2f}" if has_price else "无价格")
            pnl_pct = row["unrealized_pnl_pct"]
            has_pnl = pd.notna(pnl_pct)
            color = "🔴" if has_pnl and pnl_pct < 0 else "🟢"
            cols[4].markdown(f"**{color} {pnl_pct:.1%}**" if has_pnl else "—")
            if cols[5].button("平仓", key=f"close_{row['lot_id']}"):
                conn.execute(
                    "UPDATE positions SET is_closed = TRUE, closed_at = ? WHERE lot_id = ?",
                    [dt.date.today(), row["lot_id"]],
                )
                st.rerun()

st.divider()
st.subheader("建议卡片（基于最新一次掘金扫描 + 当前持仓状态）")
st.caption("点击下方按钮触发一次新的建议生成（会重新跑模型打分，可能需要几秒到几十秒）；生成后首页「今日决策速览」也会同步更新。")

if st.button("🔄 生成/刷新建议卡片", type="primary"):
    with st.spinner("正在跑掘金扫描 + 生成建议卡片..."):
        from advice.advice_engine import generate_daily_advice
        result = generate_daily_advice()
        st.session_state["advice_result"] = result

result = st.session_state.get("advice_result")
if result:
    st.caption(f"建议生成时间对应交易日：{result['as_of']}")
    if result["position_cards"]:
        st.markdown("### 持仓相关建议")
        for card in result["position_cards"]:
            render_advice_card(card)
    else:
        st.info("当前无持仓相关建议（可能还没有录入持仓）。")

    if result.get("watchlist_cards"):
        with st.expander(f"👀 观察名单建议（{len(result['watchlist_cards'])}条，未持有但进入掘金Top候选）"):
            for card in result["watchlist_cards"]:
                render_advice_card(card)
else:
    st.info("点击上方按钮生成建议卡片。")

conn.close()
