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
st.subheader("① 建议卡片（先看建议，再练模拟）")
advice_pool = st.radio(
    "建议池",
    ["hs", "bj"],
    format_func=lambda x: "沪深池" if x == "hs" else "北交所池",
    horizontal=True,
    key="advice_pool",
)
st.caption("本区只负责生成/查看建议。模拟成交请到下方「② 本机模拟盘」。")

if "advice_by_pool" not in st.session_state:
    st.session_state["advice_by_pool"] = {}

gen_clicked = st.button(
    "🔄 生成/刷新建议卡片",
    type="primary",
    disabled=readonly or not _decisions_ok,
    key="gen_advice",
)

if gen_clicked:
    if not _decisions_ok:
        st.error("数据未就绪，无法生成建议。请先完成日更或查看页顶说明。")
    else:
        with st.spinner(f"正在扫描{'沪深' if advice_pool == 'hs' else '北交所'}池并生成建议卡片..."):
            from advice.advice_engine import generate_daily_advice

            out = generate_daily_advice(
                allow_bj_open=st.session_state.get("allow_bj_open_in_todos"),
                pool_id=advice_pool,
            )
            st.session_state["advice_by_pool"][advice_pool] = out
            st.session_state["advice_result"] = out
            st.rerun()

result = st.session_state["advice_by_pool"].get(advice_pool)
if result is None:
    from advice.advice_engine import load_latest_advice_cards

    as_of_db, cards_db = load_latest_advice_cards(conn, pool_id=advice_pool)
    if as_of_db and cards_db:
        held = set()
        try:
            ps = compute_position_summary(conn, as_of_db)
            held = set(ps["symbol"].astype(str)) if not ps.empty else set()
        except Exception:
            pass
        result = {
            "as_of": as_of_db,
            "pool_id": advice_pool,
            "position_cards": [c for c in cards_db if c["symbol"] in held],
            "watchlist_cards": [c for c in cards_db if c["symbol"] not in held],
            "tomorrow_todos": [],
            "from_db": True,
        }
        st.session_state["advice_by_pool"][advice_pool] = result

if result:
    st.caption(
        f"池：{'沪深' if advice_pool == 'hs' else '北交所'} · 建议对应交易日：{result['as_of']}"
        + ("（来自库内快照）" if result.get("from_db") else "")
    )
    top3 = result.get("open_top3") or []
    if top3:
        st.caption(
            "可建仓候选（最多 3 只）："
            + "；".join(f"{c['symbol']} {int(c['size_shares'])}股" for c in top3)
        )

    if result["position_cards"]:
        st.markdown("### 持仓相关建议")
        for card in result["position_cards"]:
            render_advice_card(card, details="toggle")
    else:
        st.info("当前无持仓相关建议（可能尚未录入持仓）。")

    watch = result.get("watchlist_cards") or []
    if watch:
        st.markdown(f"### 👀 观察名单（{len(watch)} 条）")
        st.caption("先看表，再点选一只查看完整卡片。")
        preview = pd.DataFrame(
            [
                {
                    "代码": c.get("symbol"),
                    "名称": c.get("name") or "",
                    "动作": c.get("action"),
                    "建议股数": int(c["size_shares"]) if c.get("size_shares") else None,
                    "一句话": (c.get("plain_summary") or "")[:40],
                }
                for c in watch[:50]
            ]
        )
        st.dataframe(preview, use_container_width=True, hide_index=True)
        labels = [
            f"{c.get('symbol')} {c.get('name') or ''} · {c.get('action')}"
            for c in watch
        ]
        pick = st.selectbox(
            "点选查看完整建议卡片",
            options=list(range(len(watch))),
            format_func=lambda i: labels[i],
            key=f"watch_pick_{advice_pool}",
        )
        render_advice_card(watch[int(pick)], details="toggle")

    todos = result.get("tomorrow_todos") or []
    if todos:
        st.markdown("### 📋 本池明日待办摘要（最多 3 条）")
        st.caption("完整页见侧边栏「明日待办」。下面模拟盘的「一键练习」默认只练这些。")
        for item in todos[:3]:
            st.write(
                f"- **{item['exec_date']}** {item['symbol']} {item.get('name') or ''} "
                f"→ {item['action']}"
                + (f" {int(item['size_shares'])} 股" if item.get("size_shares") else "")
                + (" ⚠️ 暂不可执行" if item.get("blocked") else "")
            )
else:
    st.info("请先点「生成/刷新建议卡片」。若只想练模拟，也可直接去下方点「一键练习」（会顺带生成）。")

# —— ② 模拟盘：账户 + 练习按钮 + 结果 + 持仓，集中在一处 ——
st.divider()
st.subheader("② 本机模拟盘（假账本，与上方手录持仓无关）")
st.markdown(
    """
**推荐顺序**  
1. 选账户模板 → **创建新模拟账户**（没有账户时「一键练习」也会自动建）  
2. 确认上方建议池已选好 → 点 **一键练习**（生成建议 + 只模拟明日待办 ≤3）  
3. 看本区提示：**成交 / 待开盘 / 拒绝** 与净值；缺次日开盘价出现「待开盘」是正常现象  
"""
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
)
st.session_state["allow_bj_open_in_todos"] = allow_bj

tpl_labels = {
    "live_prep_10k": "一万练习账户（推荐）",
    "practice_100k": "十万练习账户",
}
tpl = st.selectbox(
    "账户模板",
    ["live_prep_10k", "practice_100k"],
    format_func=lambda x: tpl_labels.get(x, x),
    key="paper_tpl",
)
if "paper_account_id" not in st.session_state or not st.session_state.get("paper_account_id"):
    st.session_state["paper_account_id"] = latest_paper_account_id(conn)

c_acc, c_loop, c_retry = st.columns(3)
with c_acc:
    if st.button("① 创建新模拟账户", disabled=readonly, key="paper_create"):
        st.session_state["paper_account_id"] = create_paper_account(tpl)
        st.rerun()
with c_loop:
    loop_clicked = st.button(
        "② 一键练习",
        type="primary",
        disabled=readonly or not _decisions_ok,
        key="practice_loop",
        help="生成当前建议池卡片，并只模拟明日待办最多 3 条。",
    )
with c_retry:
    retry_clicked = st.button(
        "③ 仅再模拟待办",
        disabled=readonly or not bool((result or {}).get("tomorrow_todos")),
        key="batch_sim_todos",
        help="不重新扫描；对已有明日待办再跑一遍模拟（适合补行情后重试待开盘）。",
    )

aid = st.session_state.get("paper_account_id")
if aid:
    st.caption(f"当前模拟账户：`{aid}`")
else:
    st.warning("还没有模拟账户：先点「创建新模拟账户」，或直接点「一键练习」自动创建。")

if loop_clicked:
    if not _decisions_ok:
        st.error("数据未就绪，无法一键练习。")
    else:
        from advice.advice_engine import run_practice_cycle

        if not aid:
            aid = create_paper_account(tpl)
            st.session_state["paper_account_id"] = aid
        with st.spinner("正在生成建议并模拟待办成交（最多 3 条）..."):
            out = run_practice_cycle(
                aid,
                allow_bj_open=st.session_state.get("allow_bj_open_in_todos"),
                pool_id=advice_pool,
            )
            st.session_state["advice_by_pool"][advice_pool] = out
            st.session_state["advice_result"] = out
            st.session_state["last_simulation"] = out.get("simulation")
            st.session_state["paper_account_id"] = aid
            st.rerun()

if retry_clicked and aid and result and (result.get("tomorrow_todos") or []):
    todos = result["tomorrow_todos"]
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
    sm = simulate_advice_cards(aid, cards)
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

sim = st.session_state.get("last_simulation")
if sim:
    pending = int(sim.get("pending") or 0)
    banner = (
        f"最近一次模拟：成交 {sim['filled']} · 待开盘 {pending} · "
        f"跳过 {sim['skipped']} · 拒绝 {sim['rejected']} · "
        f"净值 ¥{sim['nav_before']:,.2f} → ¥{sim['nav_after']:,.2f}"
    )
    # 不用 st.success/st.info 在 rerun 后切换类型（易触发 React removeChild）
    with st.container(border=True):
        st.markdown(f"**{banner}**")
        if pending and not sim["filled"]:
            st.caption("缺执行日开盘价会记「待开盘」。行情补上后再点「③ 仅再模拟待办」。")

    reasons = sim.get("reason_counts") or {}
    lines = sim.get("lines") or []
    if reasons or lines:
        st.caption("模拟原因分布 / 明细")
        if reasons:
            st.dataframe(
                pd.DataFrame([{"原因": k, "次数": v} for k, v in reasons.items()]),
                use_container_width=True,
                hide_index=True,
            )
        if lines:
            st.dataframe(
                pd.DataFrame(
                    [
                        {
                            "状态": ln.get("status"),
                            "代码": ln.get("symbol"),
                            "动作": ln.get("action"),
                            "说明": ln.get("message") or "",
                        }
                        for ln in lines
                    ]
                ),
                use_container_width=True,
                hide_index=True,
            )

if aid:
    pos_df = list_paper_positions(conn, aid)
    if not pos_df.empty:
        st.markdown("**模拟持仓**")
        st.dataframe(pos_df, use_container_width=True, hide_index=True)
    else:
        st.caption("模拟持仓为空（尚未成交，或全是待开盘）。")
    nav = mark_to_market_nav(conn, aid, dt.date.today())
    st.metric("模拟净值（现金+持仓市值）", f"¥{nav['nav_cny']:,.2f}", f"现金 ¥{nav['cash_cny']:,.2f}")
    wr = paper_weekly_report(conn, aid)
    w1, w2, w3, w4 = st.columns(4)
    w1.metric("7 日成交笔数", wr["trade_count"])
    w2.metric(
        "7 日净值变化",
        f"{wr['return_7d']:.2%}" if wr.get("return_7d") is not None else "—",
    )
    w3.metric("7 日成交额", f"¥{wr['turnover_notional']:,.0f}")
    w4.metric("最大单票权重", f"{wr['max_single_weight']:.1%}")

    show_manual = st.checkbox(
        "高级：手动输入代码模拟一笔（一般不用）", key="paper_show_manual"
    )
    if show_manual:
        with st.form("paper_manual_fill"):
            c1, c2, c3, c4 = st.columns(4)
            ps = c1.text_input("代码", value="000001")
            side = c2.selectbox(
                "方向", ["buy", "sell"], format_func=lambda x: "买入" if x == "buy" else "卖出"
            )
            sh = c3.number_input("股数", min_value=100, step=100, value=100)
            td = c4.date_input("成交日", value=dt.date.today())
            aid_in = st.text_input("关联建议编号（可选）", value="")
            if st.form_submit_button("模拟成交", disabled=readonly):
                r = execute_paper_order(
                    aid,
                    symbol=ps.strip(),
                    side=side,  # type: ignore[arg-type]
                    shares=int(sh),
                    trade_date=td,
                    advice_id=aid_in.strip() or None,
                )
                st.session_state["paper_manual_result"] = {
                    "status": r.status,
                    "trade_id": r.trade_id,
                    "price": r.price,
                    "fees": r.fees,
                    "cash_after": r.cash_after,
                    "reject_reason": r.reject_reason,
                }
                st.rerun()
        mr = st.session_state.pop("paper_manual_result", None)
        if mr:
            with st.container(border=True):
                if mr["status"] == "filled":
                    st.markdown(
                        f"成交 `{mr['trade_id']}` 价 {mr['price']:.2f} "
                        f"费 {mr['fees']:.2f} 现金余 {mr['cash_after']:.2f}"
                    )
                else:
                    st.markdown(mr.get("reject_reason") or mr["status"])

close_warehouse(conn)
