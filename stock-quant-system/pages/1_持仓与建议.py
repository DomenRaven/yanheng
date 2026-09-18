"""持仓管理（手动录入，不接券商API） + 该持仓对应的建议卡片。"""
from __future__ import annotations

import datetime as dt
import uuid

import pandas as pd
import streamlit as st

from common.db import is_read_only
from common.symbol_lookup import render_symbol_picker
from advice.paper_broker import latest_paper_account_id
from common.ui_theme import (
    apply_theme,
    close_warehouse,
    connect_warehouse,
    render_advice_card,
    render_production_banners,
    require_warehouse_for_decisions,
    section_header,
)
from common.warehouse_readiness import assess_warehouse_readiness

apply_theme(page_title="持仓与建议", page_icon="💼")
st.title("💼 持仓与建议")

conn = connect_warehouse()
readonly = is_read_only(conn)
render_production_banners(conn)
_wh_report = assess_warehouse_readiness(conn)
_decisions_ok = _wh_report.ready

st.subheader("录入新持仓")
st.caption("代码和名称都能搜：填 000001 或「平安银行」均可。多只同名时请在下拉里选定。")
symbol = render_symbol_picker(conn, key="add_pos_symbol")
with st.form("add_position_form", clear_on_submit=True):
    c1, c2, c3 = st.columns(3)
    shares = c1.number_input("股数", min_value=0.0, step=100.0)
    cost_price = c2.number_input("建仓成本价（每股）", min_value=0.0, step=0.01, format="%.2f")
    opened_at = c3.date_input("建仓日期", value=dt.date.today())
    note = st.text_input("备注（可选）")
    submitted = st.form_submit_button("添加持仓批次", type="primary", disabled=readonly)
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
st.subheader("建议卡片（结合最近一次扫描与当前持仓）")
st.caption(
    "「生成/刷新建议卡片」只生成卡片（观察名单来自**沪深池**扫描）。"
    "「一键练习」默认只模拟**明日待办最多 3 条**（缺次日开盘价会显示待开盘，不是系统故障）。"
    "全量卡片模拟需二次确认。北交所开仓默认关闭，可在下方模拟盘区打开。"
)

btn_col1, btn_col2 = st.columns(2)
with btn_col1:
    gen_clicked = st.button(
        "🔄 生成/刷新建议卡片",
        type="primary",
        disabled=readonly or not _decisions_ok,
        key="gen_advice",
    )
with btn_col2:
    loop_clicked = st.button(
        "🎯 一键练习（生成并模拟成交）",
        disabled=readonly or not _decisions_ok,
        key="practice_loop",
    )

if gen_clicked:
    require_warehouse_for_decisions(conn)
    with st.spinner("正在扫描沪深池并生成建议卡片..."):
        from advice.advice_engine import generate_daily_advice

        st.session_state["advice_result"] = generate_daily_advice(
            allow_bj_open=st.session_state.get("allow_bj_open_in_todos")
        )
        st.session_state.pop("last_simulation", None)

if loop_clicked:
    require_warehouse_for_decisions(conn)
    from advice.advice_engine import run_practice_cycle
    from advice.paper_broker import create_paper_account, latest_paper_account_id

    aid = st.session_state.get("paper_account_id") or latest_paper_account_id(conn)
    if not aid:
        tpl = st.session_state.get("paper_tpl", "live_prep_10k")
        aid = create_paper_account(tpl)
        st.session_state["paper_account_id"] = aid
    with st.spinner("正在生成建议并模拟待办成交，可能需要几十秒..."):
        out = run_practice_cycle(
            aid,
            allow_bj_open=st.session_state.get("allow_bj_open_in_todos"),
        )
        st.session_state["advice_result"] = out
        st.session_state["last_simulation"] = out.get("simulation")
        st.session_state["paper_account_id"] = aid

result = st.session_state.get("advice_result")
if result:
    st.caption(f"建议对应交易日：{result['as_of']}")
    sim = st.session_state.get("last_simulation") or result.get("simulation")
    if sim:
        pending = int(sim.get("pending") or 0)
        scope = result.get("simulate_scope") or "todos"
        banner = (
            f"练习完成（范围：{'仅明日待办' if scope == 'todos' else '全部可执行卡片'}）："
            f"成交 {sim['filled']} · 待开盘 {pending} · 跳过 {sim['skipped']} · 拒绝 {sim['rejected']} · "
            f"模拟净值 ¥{sim['nav_before']:,.2f} → ¥{sim['nav_after']:,.2f} "
            f"（账户 `{result.get('paper_account_id', st.session_state.get('paper_account_id', '—'))}`）"
        )
        if pending and not sim["filled"]:
            st.info(
                banner
                + "\n\n说明：建议对应的是信号日收盘；模拟默认按 **次日开盘价** 成交。"
                "执行日行情尚未入库时记为「待开盘」，不是系统坏了。"
                "开盘后数据更新完毕，再点下方「按明日待办模拟」即可。"
            )
        else:
            st.success(banner)
        reasons = sim.get("reason_counts") or {}
        if reasons:
            with st.expander("模拟结果原因分布", expanded=bool(pending or sim["rejected"])):
                for msg, n in reasons.items():
                    st.write(f"- {msg} × {n}")
                lines = sim.get("lines") or []
                if lines:
                    st.caption("明细")
                    for ln in lines:
                        st.write(
                            f"`{ln.get('status')}` {ln.get('symbol')} {ln.get('action')} — {ln.get('message') or ''}"
                        )
        st.caption("练习 ≠ 荐股赚钱；拒绝/待开盘是 A 股约束（涨跌停、现金、T+1、缺价），不是按钮失灵。")
    top3 = result.get("open_top3") or []
    if top3:
        st.caption(
            "可建仓候选（最多 3 只）："
            + "；".join(f"{c['symbol']} {int(c['size_shares'])}股" for c in top3)
        )
    if result["position_cards"]:
        st.markdown("### 持仓相关建议")
        for card in result["position_cards"]:
            render_advice_card(card)
    else:
        st.info("当前无持仓相关建议（可能尚未录入持仓）。")

    if result.get("watchlist_cards"):
        with st.expander(f"👀 观察名单（{len(result['watchlist_cards'])} 条，未持有但进入扫描前列）"):
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
        from advice.paper_broker import simulate_advice_cards

        c_todo, c_all = st.columns(2)
        with c_todo:
            if st.button("▶ 按明日待办模拟成交", key="batch_sim_todos"):
                todos = result.get("tomorrow_todos") or []
                cards = [
                    {
                        "advice_id": t.get("advice_id"),
                        "symbol": t["symbol"],
                        "action": t["action"],
                        "size_shares": t.get("size_shares") or 0,
                        "exec_date": t.get("exec_date"),
                    }
                    for t in todos
                ]
                sm = simulate_advice_cards(aid_batch, cards)
                st.session_state["last_simulation"] = {
                    "filled": sm.filled,
                    "skipped": sm.skipped,
                    "rejected": sm.rejected,
                    "pending": sm.pending,
                    "nav_before": sm.nav_before,
                    "nav_after": sm.nav_after,
                    "reason_counts": sm.reason_counts(),
                    "lines": [
                        {
                            "advice_id": ln.advice_id,
                            "symbol": ln.symbol,
                            "action": ln.action,
                            "status": ln.status,
                            "message": ln.message,
                        }
                        for ln in sm.lines
                    ],
                }
                st.rerun()
        with c_all:
            confirm_all = st.checkbox("确认：模拟全部可执行建议（可能大量现金不足）", key="confirm_sim_all")
            if st.button("⚡ 全量建议模拟", key="batch_sim_today", disabled=not confirm_all):
                all_cards = result["position_cards"] + result.get("watchlist_cards", [])
                sm = simulate_advice_cards(aid_batch, all_cards)
                st.session_state["last_simulation"] = {
                    "filled": sm.filled,
                    "skipped": sm.skipped,
                    "rejected": sm.rejected,
                    "pending": sm.pending,
                    "nav_before": sm.nav_before,
                    "nav_after": sm.nav_after,
                    "reason_counts": sm.reason_counts(),
                    "lines": [
                        {
                            "advice_id": ln.advice_id,
                            "symbol": ln.symbol,
                            "action": ln.action,
                            "status": ln.status,
                            "message": ln.message,
                        }
                        for ln in sm.lines
                    ],
                }
                st.rerun()
else:
    st.info("请点击上方按钮生成建议卡片。")

st.divider()
with st.expander("🧪 本机模拟盘（与上方手录持仓分开）", expanded=False):
    st.caption(
        "练习账户默认按次日开盘价加减配置滑点成交，不含真实盘口。"
        "当日买入的股票受 T+1（当日买入、次日方可卖出）约束，模拟盘会拒绝当日卖出。"
        "缺执行日开盘价时记为「待开盘」，不算拒绝。"
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
    from common.config import get_config

    pt_cfg = get_config().get("paper_trading") or {}
    allow_bj = st.checkbox(
        "允许明日待办纳入北交所开仓（默认关）",
        value=bool(pt_cfg.get("allow_bj_open_in_todos", False)),
        key="allow_bj_open_ui",
        help="打开后，下次「生成建议」时待办可能出现北交所；仍受单票仓位与同时持股上限约束。",
    )
    st.session_state["allow_bj_open_in_todos"] = allow_bj
    if allow_bj != bool(pt_cfg.get("allow_bj_open_in_todos", False)):
        st.caption("本选项仅对当前浏览器会话生效；持久化请改 config.yaml → paper_trading.allow_bj_open_in_todos。")

    tpl_labels = {
        "live_prep_10k": "一万练习账户",
        "practice_100k": "十万练习账户",
    }
    tpl = st.selectbox(
        "账户模板",
        ["live_prep_10k", "practice_100k"],
        format_func=lambda x: tpl_labels.get(x, x),
        key="paper_tpl",
    )
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
        st.caption(f"冠军模型编号：{wr.get('model_run_id') or '—'}")
        with st.form("paper_manual_fill"):
            c1, c2, c3, c4 = st.columns(4)
            ps = c1.text_input("代码", value="000001")
            side = c2.selectbox("方向", ["buy", "sell"], format_func=lambda x: "买入" if x == "buy" else "卖出")
            sh = c3.number_input("股数", min_value=100, step=100, value=100)
            td = c4.date_input("成交日", value=dt.date.today())
            aid_in = st.text_input("关联建议编号（可选，用于避免重复成交）", value="")
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
                elif r.status == "pending":
                    st.info(r.reject_reason or "待开盘后再练")
                elif r.status == "skipped":
                    st.warning(r.reject_reason or "已跳过")
                else:
                    st.error(r.reject_reason or "已拒绝")
                st.rerun()

close_warehouse(conn)
