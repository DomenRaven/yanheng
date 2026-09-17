"""
Phase 4 Streamlit 入口——持仓驾驶舱总览页。

**产品边界（严格遵守，对齐 `docs/00-总览/02-产品定位与边界.md` 的Won't清单）**：
本应用不接券商API、不自动下单、不承诺收益。所有"建议"都是研究辅助信息，最终交易决策
及风险由使用者自行承担——每个建议卡片都带这句免责声明，本页顶部再次强调一次。

启动方式：`streamlit run app.py`（工作目录为项目根目录，需要先激活 `.venv`），
或双击项目根目录下打包好的一键启动器（见 `docs/开发者使用说明书.docx` 打包章节）。
"""
from __future__ import annotations

import datetime as dt
import json

import pandas as pd
import streamlit as st

from advice.advice_engine import build_priority_digest, cards_for_digest, load_latest_advice_cards
from common.symbol_lookup import render_symbol_picker
from common.ui_theme import apply_theme, connect_warehouse, render_advice_card, close_warehouse, section_header
from risk.portfolio_risk import compute_concentration, compute_position_summary

apply_theme(page_title="研衡 YanHeng · 持仓驾驶舱", page_icon="📈", layout="wide")

st.title("📈 研衡 YanHeng —— 持仓驾驶舱")
st.caption(
    "本工具全部输出仅供个人研究辅助，不构成投资建议；不接入券商账户、不自动下单，"
    "最终交易决策及风险由使用者自行承担。"
)

conn = connect_warehouse()
try:
    today = dt.date.today().strftime("%Y-%m-%d")
    position_summary = compute_position_summary(conn, today)
    concentration = compute_concentration(position_summary)

    n_positions = concentration["symbol"].nunique() if not concentration.empty else 0
    total_market_value = float(position_summary["market_value"].sum()) if not position_summary.empty else 0.0
    total_pnl = float(position_summary["unrealized_pnl"].sum()) if not position_summary.empty else 0.0

    latest_champion = None
    try:
        with open("mlops/registry/champion.json", encoding="utf-8") as f:
            latest_champion = json.load(f)
    except FileNotFoundError:
        pass

    col1, col2, col3, col4 = st.columns(4)
    col1.metric("持仓股票数", n_positions)
    col2.metric("持仓市值合计", f"¥{total_market_value:,.0f}" if total_market_value else "—")
    col3.metric(
        "浮动盈亏合计",
        f"¥{total_pnl:,.0f}" if position_summary is not None and not position_summary.empty else "—",
        delta=f"{total_pnl:,.0f}" if total_pnl else None,
    )
    col4.metric("生产冠军模型", latest_champion["run_id"] if latest_champion else "未设置")

    st.divider()

    # ---- 今日决策速览：把最近一次「持仓与建议」页生成的结果，按紧急程度汇总展示 ----
    st.subheader("🎯 今日决策速览")
    st.caption(
        "按紧急程度排序：止损/减仓优先。本页每次打开都重新读库；"
        "已平仓的股票不会再显示持仓类建议。无持仓时这里只展示观察名单候选，不是「继续持有/减仓」。"
    )
    as_of, latest_cards = load_latest_advice_cards(conn)
    held_symbols = set(concentration["symbol"].tolist()) if not concentration.empty else set()
    latest_cards = cards_for_digest(latest_cards, held_symbols)
    if not latest_cards:
        st.info(
            "还没有可展示的今日决策。前往左侧「持仓与建议」录入持仓并点击「生成/刷新建议卡片」。"
        )
    else:
        digest = build_priority_digest(latest_cards, [], top_n=6)
        urgent = [c for c in digest if c.get("is_urgent")]
        if urgent:
            st.warning(f"⚠️ 有 {len(urgent)} 条需要重点关注的风控提示（止损/减仓/止盈/再平衡），建议今天处理。")
        elif n_positions == 0:
            st.info("当前没有持仓。下列是观察名单（模型排序靠前、尚未持有），不是对已有仓位的操作建议。")
        st.caption(f"数据对应交易日：{as_of}（在「持仓与建议」页再次生成可整份替换当天快照）")
        for card in digest:
            render_advice_card(card)

    st.divider()

    section_header("持仓明细（按symbol聚合）", help_term="集中度", level=2)
    if position_summary.empty:
        st.info(
            "还没有录入任何持仓。前往左侧「持仓与建议」页面手动录入你的持仓，"
            "或直接查看「掘金扫描」页面获取全市场候选标的。"
        )
        st.caption("有持仓后，本表会显示各股票市值占比。点上方标题右侧「❓ 集中度」可看名词解释。")
    else:
        display = concentration.merge(
            position_summary.groupby("symbol", as_index=False).agg(
                unrealized_pnl=("unrealized_pnl", "sum"),
            ),
            on="symbol", how="left",
        )
        display["weight_pct"] = (display["weight_pct"] * 100).round(2)
        st.dataframe(
            display.rename(columns={
                "symbol": "股票代码", "market_value": "市值", "weight_pct": "占比(%)",
                "unrealized_pnl": "浮动盈亏",
            }),
            use_container_width=True,
            hide_index=True,
        )
        max_weight = display["weight_pct"].max() if not display.empty else 0
        if max_weight > 15:
            st.warning(f"⚠️ 单票集中度已达 {max_weight:.1f}%，超过15%风控上限，建议前往「风险仪表盘」页面查看详情。")

    st.divider()
    st.subheader("🔍 按名称查代码")
    st.caption("本地股票池检索，不联网。输入「紫金矿业」「中信证券」或 6 位代码均可。")
    render_symbol_picker(conn, key="home_lookup")
finally:
    close_warehouse(conn)

st.divider()
st.markdown(
    "**导航**：「持仓与建议」录入/管理持仓并查看建议卡片 · 「掘金扫描」查看全市场模型排名 · "
    "「行情图表」看K线与持仓成本/止盈止损参考线 · 「风险仪表盘」组合风险指标 · "
    "「AI解释」用通义千问把结构化信号翻译成人话（需配置API Key；「今天」以本机日期为准） · "
    "「历史建议复盘」核对过去建议后续走势 · 「名词解释」大白话说明专业术语。\n\n"
    "详细方法论与验收记录见项目根目录 `docs/phase*-acceptance-report.md`。"
)
