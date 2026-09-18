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

import pandas as pd
import streamlit as st

from advice.advice_engine import build_priority_digest, cards_for_digest, load_latest_advice_cards
from advice.champion_registry import describe_champion
from common.symbol_lookup import render_symbol_picker
from common.ui_theme import (
    apply_theme,
    close_warehouse,
    connect_warehouse,
    render_advice_card,
    render_production_banners,
    section_header,
    term_help,
)
from risk.portfolio_risk import compute_concentration, compute_position_summary

apply_theme(page_title="研衡 · 持仓驾驶舱", page_icon="📈", layout="wide")

st.title("📈 研衡 —— 持仓驾驶舱")
st.caption(
    "本工具全部输出仅供个人研究辅助；不接入券商账户、不自动下单。"
    "最终交易决策及风险由使用者自行承担。"
)

conn = connect_warehouse()
try:
    render_production_banners(conn)
    today = dt.date.today().strftime("%Y-%m-%d")
    position_summary = compute_position_summary(conn, today)
    concentration = compute_concentration(position_summary)

    n_positions = concentration["symbol"].nunique() if not concentration.empty else 0
    total_market_value = float(position_summary["market_value"].sum()) if not position_summary.empty else 0.0
    total_pnl = float(position_summary["unrealized_pnl"].sum()) if not position_summary.empty else 0.0

    hs = describe_champion(pool_id="hs")
    bj = describe_champion(pool_id="bj")

    col1, col2, col3, col4, col5 = st.columns(5)
    col1.metric("持仓股票数", n_positions)
    col2.metric("持仓市值合计", f"¥{total_market_value:,.0f}" if total_market_value else "—")
    col3.metric(
        "浮动盈亏合计",
        f"¥{total_pnl:,.0f}" if position_summary is not None and not position_summary.empty else "—",
        delta=f"{total_pnl:,.0f}" if total_pnl else None,
    )
    col4.metric("沪深池冠军", hs["run_id"] or "未设置")
    col5.metric("北交所池冠军", bj["run_id"] or "未就绪")
    with st.expander("如何区分两池冠军？（点开说明）", expanded=False):
        term_help("冠军模型")
        st.markdown(
            f"""
| 池 | 指针文件 | 当前 run | 训练面板区间 | 样本外 RankIC |
|----|----------|----------|--------------|---------------|
| 沪深（sh+sz） | `champion_hs.json` | `{hs['run_id'] or '—'}` | `{hs['date_range'] or '—'}` | `{hs['walk_forward_rank_ic'] if hs['walk_forward_rank_ic'] is not None else '—'}` |
| 北交所（bj） | `champion_bj.json` | `{bj['run_id'] or '—'}` | `{bj['date_range'] or '—'}` | `{bj['walk_forward_rank_ic'] if bj['walk_forward_rank_ic'] is not None else '—'}` |

- **谁在用**：掘金选「沪深池」只加载上表沪深行；选「北交所池」只加载北交所行。建议引擎观察名单默认走沪深池。
- **怎么当上冠军**：挑战者用同一套 Walk-Forward 与现任比；达标才改指针。不是「文件夹日期最新」。
- **和日频掘金日期不同**：训练面板按月末截面（上表区间）；掘金用最近交易日快照（见页脚数据截止）。
"""
        )

    st.divider()

    st.subheader("🎯 今日决策速览")
    digest_pool = st.radio(
        "速览池",
        ["hs", "bj"],
        format_func=lambda x: "沪深池" if x == "hs" else "北交所池",
        horizontal=True,
        key="home_digest_pool",
    )
    st.caption(
        "按紧急程度排序：止损、减仓优先。切换池只显示该池已生成的建议快照。"
        "本页只读摘要；要生成建议或练模拟，请到左侧「持仓与建议」。"
    )
    from common.warehouse_readiness import assess_warehouse_readiness

    wh_ok = assess_warehouse_readiness(conn).ready
    as_of, latest_cards = load_latest_advice_cards(conn, pool_id=digest_pool)
    held_symbols = set(concentration["symbol"].tolist()) if not concentration.empty else set()
    latest_cards = cards_for_digest(latest_cards, held_symbols)
    if not wh_ok:
        st.warning("数据未达可用门槛，今日决策速览暂不展示（请先按页顶说明完成数据更新）。")
    elif not latest_cards:
        st.info(
            f"「{'沪深' if digest_pool == 'hs' else '北交所'}池」还没有可展示的今日决策。"
            "请前往「持仓与建议」切换同池并点击「生成/刷新建议卡片」。"
        )
    else:
        digest = build_priority_digest(latest_cards, [], top_n=6)
        urgent = [c for c in digest if c.get("is_urgent")]
        if urgent:
            st.warning(f"有 {len(urgent)} 条需要重点关注的提示（止损、减仓、止盈或再平衡），建议今日优先处理。")
        elif n_positions == 0:
            st.info("当前没有持仓。下列为观察名单（模型排序靠前、尚未持有），供您自行研究。")
        st.caption(
            f"池：{'沪深' if digest_pool == 'hs' else '北交所'} · 数据对应交易日：{as_of}"
            "（在「持仓与建议」页再次生成，可整份替换当天该池快照）"
        )
        for card in digest:
            render_advice_card(card)

    st.divider()

    section_header("持仓明细（按代码汇总）", help_term="集中度", level=2)
    if position_summary.empty:
        st.info(
            "还没有录入任何持仓。请前往左侧「持仓与建议」手动录入，"
            "或打开「掘金扫描」查看全市场候选。"
        )
        st.caption("有持仓后，本表会显示各股票市值占比。点标题右侧「❓ 集中度」可看名词解释。")
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
            st.warning(f"单票集中度已达 {max_weight:.1f}%，超过 15% 上限，建议前往「风险仪表盘」查看详情。")

    st.divider()
    st.subheader("🔍 按名称查代码")
    st.caption("本地股票池检索，不联网。可输入公司名称（如「紫金矿业」）或 6 位代码。")
    render_symbol_picker(conn, key="home_lookup")
finally:
    close_warehouse(conn)

st.divider()
st.markdown(
    "**页面导航**：「持仓与建议」①生成建议 ②模拟盘练习 · 「明日待办」次日最多 3 条 · "
    "「掘金扫描」分池排序 · 「历史建议复盘」事后对照 · 「风险 / 行情 / AI / 名词」。\n\n"
    "生成建议前，请确认左侧「数据状态」为可用。"
)
