"""
历史建议复盘——读 `advice_log` 核对"过去系统给出的建议，后续价格实际走势如何"，
建立对系统的信任基础（而不是只能盲信"模型打分高"这句话）。

**如实说明局限**：本页只统计"建议发出日收盘价 -> 最新收盘价"的涨跌幅，不是严格的
"如果照做能赚多少钱"回测（没有考虑真实成交价、滑点、仓位大小、多笔建议的组合效应）——
真正严谨的策略级回测见 `research/backtest.py` 和 `docs/phase1-acceptance-report.md`。
本页是给"个人决策参考"用的简化复盘，不是产品收益承诺。
"""
from __future__ import annotations

import datetime as dt

import pandas as pd
import streamlit as st

from advice.advice_trace import complete_trace_count, load_advice_traces
from common.db import is_read_only
from common.ui_theme import action_meta, apply_theme, connect_warehouse, close_warehouse, render_production_banners, section_header

apply_theme(page_title="历史建议复盘", page_icon="🕰️")
st.title("🕰️ 历史建议复盘")
st.caption(
    "统计方式：从「建议发出当天收盘价」到「最新收盘价」的涨跌幅，用来大致核对过去建议的方向。"
    "未计入真实成交价、滑点、仓位大小与组合效应；请勿当作未来收益承诺。"
)

conn = connect_warehouse()
try:
    render_production_banners(conn)
    traces = load_advice_traces(conn)
    n_complete = complete_trace_count(traces)
    m1, m2 = st.columns(2)
    m1.metric("建议条数", 0 if traces.empty else len(traces))
    m2.metric("已关联成交的建议", n_complete)
    st.caption(
        "「已关联成交」指：同一条建议既有快照，又有成功成交（模拟或您确认的实盘）。"
        "显示为 0 表示尚未走过「生成建议 → 模拟或确认成交」。"
        "成交价相对建议日收盘的价差，反映日频执行缺口。"
    )

    log = conn.execute(
        """
        SELECT advice_id, as_of, symbol, name, action, confidence, plain_summary, created_at,
               size_shares, est_amount_cny, exec_date, max_loss_cny
        FROM advice_log
        ORDER BY as_of DESC, created_at DESC
        """
    ).df()

    if log.empty:
        st.info("还没有任何历史建议记录。前往「持仓与建议」页点击「生成/刷新建议卡片」后，记录会自动累积在这里。")
    else:
        log["as_of"] = pd.to_datetime(log["as_of"]).dt.date
        n_days = log["as_of"].nunique()
        st.caption(f"当前累计 {n_days} 个交易日、{len(log)} 条建议记录（累计天数越多，复盘越有参考价值）。")
        if n_days < 5:
            st.warning("目前累计天数还比较少，复盘结论仅供参考，建议持续使用几周后再重点参考本页。")

        symbols = log["symbol"].unique().tolist()
        placeholders = ",".join(["?"] * len(symbols))
        prices = conn.execute(
            f"""
            SELECT symbol, trade_date, close
            FROM daily_quotes
            WHERE symbol IN ({placeholders}) AND adjust = 'qfq'
            ORDER BY symbol, trade_date
            """,
            symbols,
        ).df()
        # DuckDB DATE列经pandas转换后精度可能是[s]，而log["as_of"]转换后是[us]，
        # merge_asof要求左右key的datetime64精度完全一致——这是2026-08-26用真实数据
        # 跑通本页时实测到的报错(MergeError)，不是猜测，统一转成[us]规避。
        prices["trade_date"] = pd.to_datetime(prices["trade_date"]).astype("datetime64[us]")

        latest_price = prices.groupby("symbol", as_index=False).tail(1).set_index("symbol")["close"]
        latest_date = prices["trade_date"].max()

        log["as_of_ts"] = pd.to_datetime(log["as_of"]).astype("datetime64[us]")
        merged_parts = []
        for symbol, grp in log.groupby("symbol"):
            # 只取trade_date/close两列参与merge_asof——若把prices的symbol列也带进来，
            # 会和log自身的symbol列重名，merge_asof默认加_x/_y后缀，导致下游"symbol"
            # 列取不到(2026-08-26真实跑通本页时实测到的KeyError，不是猜测)。
            p = prices.loc[prices["symbol"] == symbol, ["trade_date", "close"]].sort_values("trade_date")
            if p.empty:
                continue
            g = grp.sort_values("as_of_ts")
            m = pd.merge_asof(g, p, left_on="as_of_ts", right_on="trade_date", direction="backward")
            merged_parts.append(m)
        merged = pd.concat(merged_parts, ignore_index=True) if merged_parts else log.iloc[0:0]

        if merged.empty:
            st.warning("暂无法匹配到对应的历史价格数据。")
        else:
            merged["price_then"] = merged["close"]
            merged["price_now"] = merged["symbol"].map(latest_price)
            merged["pct_change_since"] = merged["price_now"] / merged["price_then"] - 1

            action_filter = st.multiselect(
                "按建议类型筛选", options=sorted(merged["action"].unique().tolist()),
                default=sorted(merged["action"].unique().tolist()),
            )
            view = merged[merged["action"].isin(action_filter)].sort_values("as_of_ts", ascending=False)

            section_header(
                f"逐条复盘（最新价格截止 {latest_date.date() if pd.notna(latest_date) else '—'}）",
                help_term="置信度", level=2,
            )

            if view.empty:
                st.info("没有符合当前筛选条件的记录，试试在上方勾选至少一种建议类型。")

            for _, row in view.head(60).iterrows():
                meta = action_meta(row["action"])
                pct = row["pct_change_since"]
                good = (pct is not None and pd.notna(pct)) and (
                    (row["action"] in ("open", "hold", "take_profit", "rebalance") and pct > 0)
                    or (row["action"] in ("stop_loss", "reduce") and pct < 0)
                )
                verdict = "✅ 方向正确" if pd.notna(pct) and good else ("🔻 方向不利" if pd.notna(pct) else "—")
                with st.container(border=True):
                    c1, c2, c3, c4 = st.columns([2, 2, 2, 2])
                    c1.markdown(
                        f"<span style='background:{meta['bg']};color:{meta['color']};padding:2px 8px;"
                        f"border-radius:6px;font-weight:700;'>{meta['emoji']} {meta['label']}</span>",
                        unsafe_allow_html=True,
                    )
                    # 注意：pandas的NaN是Python意义上的"真值"，`row.get('name') or ''`对
                    # 缺失name的旧记录（迁移前生成、没有name列数据）会把NaN原样拼进字符串
                    # 显示成字面的"nan"——2026-08-26复核历史数据时实测发现，用pd.notna()显式判断修复。
                    display_name = row.get("name") if pd.notna(row.get("name")) else ""
                    c1.write(f"**{row['symbol']} {display_name}**")
                    c2.write(f"建议日：{row['as_of']}")
                    c3.write(f"发出时¥{row['price_then']:.2f} → 现在¥{row['price_now']:.2f}" if pd.notna(row["price_then"]) else "—")
                    c4.markdown(f"涨跌幅：**{pct:.1%}**　{verdict}" if pd.notna(pct) else "暂无最新价")
                    if pd.notna(row.get("plain_summary")) and row.get("plain_summary"):
                        st.caption(f"当时的建议：{row['plain_summary']}")
                    if pd.notna(row.get("size_shares")) and row.get("size_shares"):
                        st.caption(
                            f"当时建议股数 {int(row['size_shares'])} 股"
                            + (
                                f"，约 ¥{float(row['est_amount_cny']):,.0f}"
                                if pd.notna(row.get("est_amount_cny"))
                                else ""
                            )
                        )
                    tr = traces[traces["advice_id"] == row["advice_id"]] if not traces.empty else traces
                    linked = (not tr.empty) and bool(tr.iloc[0].get("trace_complete"))
                    if linked:
                        f0 = tr.iloc[0]
                        src = "模拟" if f0["fill_source"] == "sim" else "用户确认实盘"
                        fill_px = float(f0["fill_price"])
                        gap = f0.get("fill_vs_advice_close")
                        px_note = (
                            f" · 相对建议日收盘 {float(gap):+.2%}"
                            if pd.notna(gap)
                            else ""
                        )
                        st.success(
                            f"已关联成交（{src}）：{f0['fill_side']} {int(f0['fill_shares'])} 股 "
                            f"@ ¥{fill_px:.2f}（{f0['fill_date']}）{px_note}"
                        )
                    elif row["action"] in ("open", "reduce", "stop_loss", "take_profit", "rebalance"):
                        st.caption("尚未关联模拟或确认成交记录。")

        complete = traces[traces["trace_complete"]] if not traces.empty else traces
        if not complete.empty:
            section_header("计划仓位与实际成交对照", level=2)
            st.caption(
                "对照计划股数与实际成交股数，有助于发现「想买却没买到」等情况。"
                "股数缺口 = 成交股数 − 建议股数；未成交的不在本表。"
            )
            ledger = complete.copy()
            st.dataframe(
                ledger[
                    [
                        "as_of",
                        "symbol",
                        "action",
                        "planned_shares",
                        "fill_shares",
                        "shares_gap",
                        "planned_amount_cny",
                        "fill_source",
                    ]
                ]
                .head(30)
                .rename(
                    columns={
                        "as_of": "建议日",
                        "planned_shares": "计划股数",
                        "fill_shares": "成交股数",
                        "shares_gap": "股数缺口",
                        "planned_amount_cny": "计划金额",
                        "fill_source": "来源",
                    }
                ),
                use_container_width=True,
                hide_index=True,
            )
        if not complete.empty:
            section_header("建议价与成交价偏差", level=2)
            show = complete[complete["fill_vs_advice_close"].notna()]
            if not show.empty:
                st.caption(
                    f"共 {len(show)} 条可对比；中位偏差 "
                    f"{show['fill_vs_advice_close'].median():+.2%}。"
                    "该偏差相对「信号日收盘立刻成交」的假设，用于观察执行缺口。"
                )
                st.dataframe(
                    show[
                        [
                            "as_of",
                            "symbol",
                            "action",
                            "advice_close",
                            "fill_price",
                            "fill_vs_advice_close",
                            "fill_source",
                        ]
                    ]
                    .head(30)
                    .rename(
                        columns={
                            "as_of": "建议日",
                            "advice_close": "建议日收盘",
                            "fill_price": "成交价",
                            "fill_vs_advice_close": "成交/收盘-1",
                            "fill_source": "来源",
                        }
                    ),
                    use_container_width=True,
                    hide_index=True,
                )

    section_header("券商已成交？回写到本机账本", level=2)
    st.caption(
        "在券商软件手动下单后，可将实际成交价写入本机模拟账本，便于与建议价对比。"
        "本操作不会连接券商。"
    )
    readonly = is_read_only(conn)
    from advice.paper_broker import execute_paper_order, latest_paper_account_id

    aid = latest_paper_account_id(conn)
    if not aid:
        st.info("请先在「持仓与建议 → 本机模拟盘」创建练习账户。")
    else:
        with st.form("confirm_live_fill"):
            c1, c2, c3 = st.columns(3)
            aid_in = c1.text_input("建议编号", value="")
            sym = c2.text_input("代码", value="")
            side = c3.selectbox("方向", ["buy", "sell"], format_func=lambda x: "买入" if x == "buy" else "卖出")
            c4, c5, c6 = st.columns(3)
            sh = c4.number_input("股数", min_value=100, step=100, value=100)
            px = c5.number_input("实际成交价", min_value=0.01, step=0.01, format="%.2f")
            td = c6.date_input("成交日", value=dt.date.today())
            if st.form_submit_button("确认写入", disabled=readonly):
                r = execute_paper_order(
                    aid,
                    symbol=sym.strip(),
                    side=side,  # type: ignore[arg-type]
                    shares=int(sh),
                    trade_date=td,
                    advice_id=aid_in.strip() or None,
                    source="user_confirmed_live",
                    price_override=float(px),
                )
                if r.status == "filled":
                    st.success(f"已记录 {r.trade_id}")
                elif r.status == "skipped":
                    st.warning(r.reject_reason or "已跳过")
                else:
                    st.error(r.reject_reason or "已拒绝")
                st.rerun()
finally:
    close_warehouse(conn)
