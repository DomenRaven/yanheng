"""持仓管理（手动录入，不接券商API） + 该持仓对应的建议卡片。"""
from __future__ import annotations

import datetime as dt
import uuid

import pandas as pd
import streamlit as st

from common.db import is_read_only
from common.symbol_lookup import render_symbol_picker
from advice.paper_broker import latest_paper_account_id
from common.ui_theme import apply_theme, connect_warehouse, render_advice_card, render_trust_footer, section_header

apply_theme(page_title="持仓与建议", page_icon="💼")
st.title("💼 持仓与建议")

conn = connect_warehouse()
readonly = is_read_only(conn)

st.subheader("录入新持仓")
st.caption("代码和名称都能搜：填 000001 或「平安银行」均可。多只同名时请在下拉里选定。")
symbol = render_symbol_picker(conn, key="add_pos_symbol")
with st.form("add_position_form", clear_on_submit=True):
    c1, c2, c3 = st.columns(3)
    shares = c1.number_input("股数", min_value=0.0, step=100.0)
    cost_price = c2.number_input("建仓成本价（每股）", min_value=0.0, step=0.01, format="%.2f")
    opened_at = c3.date_input("建仓日期", value=dt.date.today())
    note = st.text_input("备注（可选）")
    submitted = st.form_submit_button("添加持仓批次", type="primary")
    if submitted:
        if readonly:
            st.error("数据正在后台更新，暂时不能写入持仓。请过几秒刷新本页再试。")
        elif not symbol or shares <= 0 or cost_price <= 0:
            st.error("请先用上方搜索框选定股票，且股数、成本价需大于0")
        else:
            conn.execute(
                "INSERT INTO positions (lot_id, symbol, shares, cost_price, opened_at, note) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                [str(uuid.uuid4()), symbol, shares, cost_price, opened_at, note or None],
            )
            st.success(f"已添加持仓：{symbol} {shares}股 @ {cost_price}")
            st.rerun()

st.divider()
section_header("当前持仓（未平仓批次）", help_term="集中度", level=2)

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
            if cols[5].button("平仓", key=f"close_{row['lot_id']}", disabled=readonly):
                conn.execute(
                    "UPDATE positions SET is_closed = TRUE, closed_at = ? WHERE lot_id = ?",
                    [dt.date.today(), row["lot_id"]],
                )
                st.rerun()

st.divider()
st.subheader("建议卡片（基于最新一次掘金扫描 + 当前持仓状态）")
st.caption("点击下方按钮触发一次新的建议生成（会重新跑模型打分，可能需要几秒到几十秒）；生成后首页「今日决策速览」也会同步更新。")

if st.button("🔄 生成/刷新建议卡片", type="primary", disabled=readonly):
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
    todos = result.get("tomorrow_todos") or []
    if todos:
        st.markdown("### 📋 明日待办（最多 3 条）")
        st.caption("完整列表见侧边栏「明日待办」页。")
        for item in todos[:3]:
            st.write(
                f"- **{item['exec_date']}** {item['symbol']} {item.get('name') or ''} "
                f"→ {item['action']}"
                + (f" {int(item['size_shares'])} 股" if item.get("size_shares") else "")
                + (" ⚠️ 暂不可执行" if item.get("blocked") else "")
            )
    aid_batch = st.session_state.get("paper_account_id") or latest_paper_account_id(conn)
    if aid_batch and not readonly:
        if st.button("⚡ 按今日建议批量模拟成交", key="batch_sim_today"):
            from advice.paper_broker import simulate_advice_cards

            all_cards = result["position_cards"] + result.get("watchlist_cards", [])
            sm = simulate_advice_cards(aid_batch, all_cards)
            st.success(
                f"批量模拟完成：成交 {sm.filled} · 跳过 {sm.skipped} · 拒绝 {sm.rejected} · "
                f"净值 ¥{sm.nav_before:,.2f} → ¥{sm.nav_after:,.2f}"
            )
            with st.expander("成交明细"):
                for ln in sm.lines:
                    st.write(f"{ln.status} {ln.symbol} {ln.action} — {ln.message or ''}")
else:
    st.info("点击上方按钮生成建议卡片。")

st.divider()
with st.expander("🧪 本机模拟盘（与上方手动持仓分离）", expanded=False):
    st.caption(
        "Phase 5 练习账户：默认次日开盘价 ± 配置滑点成交，**非真实盘口**。"
        " 数据截止以行情表为准。"
    )
    from advice.paper_broker import (
        create_paper_account,
        execute_paper_order,
        latest_paper_account_id,
        list_paper_positions,
        mark_to_market_nav,
        paper_weekly_report,
        simulate_advice_cards,
    )

    tpl = st.selectbox("账户模板", ["practice_100k", "live_prep_10k"], key="paper_tpl")
    if "paper_account_id" not in st.session_state:
        st.session_state["paper_account_id"] = None
    if st.button("创建新模拟账户", disabled=readonly, key="paper_create"):
        st.session_state["paper_account_id"] = create_paper_account(tpl)
        st.success(f"已创建 {st.session_state['paper_account_id']}")
    aid = st.session_state.get("paper_account_id")
    if aid:
        st.write(f"当前账户：`{aid}`")
        pos_df = list_paper_positions(conn, aid)
        if not pos_df.empty:
            st.dataframe(pos_df, use_container_width=True)
        nav = mark_to_market_nav(conn, aid, dt.date.today())
        st.metric("模拟净值（现金+持仓市值）", f"¥{nav['nav_cny']:,.2f}", f"现金 ¥{nav['cash_cny']:,.2f}")
        wr = paper_weekly_report(conn, aid)
        st.markdown("**近 7 日模拟盘摘要**")
        w1, w2, w3, w4 = st.columns(4)
        w1.metric("7 日成交笔数", wr["trade_count"])
        w2.metric(
            "7 日净值变化",
            f"{wr['return_7d']:.2%}" if wr.get("return_7d") is not None else "—",
        )
        w3.metric("7 日成交额", f"¥{wr['turnover_notional']:,.0f}")
        w4.metric("最大单票权重", f"{wr['max_single_weight']:.1%}")
        st.caption(f"model_run_id：{wr.get('model_run_id') or '—'}")
        with st.form("paper_manual_fill"):
            c1, c2, c3, c4 = st.columns(4)
            ps = c1.text_input("代码", value="000001")
            side = c2.selectbox("方向", ["buy", "sell"])
            sh = c3.number_input("股数", min_value=100, step=100, value=100)
            td = c4.date_input("成交日", value=dt.date.today())
            aid_in = st.text_input("关联 advice_id（可选，幂等）", value="")
            if st.form_submit_button("模拟成交", disabled=readonly):
                r = execute_paper_order(
                    aid,
                    symbol=ps.strip(),
                    side=side,  # type: ignore[arg-type]
                    shares=int(sh),
                    trade_date=td,
                    advice_id=aid_in.strip() or None,
                )
                if r.status == "filled":
                    st.success(f"成交 {r.trade_id} 价 {r.price:.2f} 费 {r.fees:.2f} 现金余 {r.cash_after:.2f}")
                elif r.status == "skipped":
                    st.warning(r.reject_reason or "已跳过")
                else:
                    st.error(r.reject_reason or "拒绝")
                st.rerun()

render_trust_footer(conn)
conn.close()
