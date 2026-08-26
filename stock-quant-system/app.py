"""
Phase 4 Streamlit 入口——持仓驾驶舱总览页。

**产品边界（严格遵守，对齐 `docs/00-总览/02-产品定位与边界.md` 的Won't清单）**：
本应用不接券商API、不自动下单、不承诺收益。所有"建议"都是研究辅助信息，最终交易决策
及风险由使用者自行承担——每个建议卡片都带这句免责声明，本页顶部再次强调一次。

启动方式：`streamlit run app.py`（工作目录为项目根目录，需要先激活 `.venv`）。
"""
from __future__ import annotations

import datetime as dt

import pandas as pd
import streamlit as st

from common.db import get_connection, init_schema
from risk.portfolio_risk import compute_concentration, compute_position_summary

st.set_page_config(page_title="个人量化研究系统", page_icon="📈", layout="wide")

st.title("📈 个人量化研究系统 —— 持仓驾驶舱")
st.caption(
    "本工具全部输出仅供个人研究辅助，不构成投资建议；不接入券商账户、不自动下单，"
    "最终交易决策及风险由使用者自行承担。"
)

conn = get_connection()
init_schema(conn)
try:
    today = dt.date.today().strftime("%Y-%m-%d")
    position_summary = compute_position_summary(conn, today)
    concentration = compute_concentration(position_summary)

    n_positions = concentration["symbol"].nunique() if not concentration.empty else 0
    total_market_value = float(position_summary["market_value"].sum()) if not position_summary.empty else 0.0
    total_pnl = float(position_summary["unrealized_pnl"].sum()) if not position_summary.empty else 0.0

    latest_champion = None
    try:
        import json
        with open("mlops/registry/champion.json", encoding="utf-8") as f:
            latest_champion = json.load(f)
    except FileNotFoundError:
        pass

    col1, col2, col3, col4 = st.columns(4)
    col1.metric("持仓股票数", n_positions)
    col2.metric("持仓市值合计", f"¥{total_market_value:,.0f}" if total_market_value else "—")
    col3.metric("浮动盈亏合计", f"¥{total_pnl:,.0f}" if position_summary is not None and not position_summary.empty else "—",
                delta=f"{total_pnl:,.0f}" if total_pnl else None)
    col4.metric("生产冠军模型", latest_champion["run_id"] if latest_champion else "未设置")

    st.divider()

    if position_summary.empty:
        st.info("还没有录入任何持仓。前往左侧「持仓与建议」页面手动录入你的持仓，"
                "或直接查看「掘金扫描」页面获取全市场候选标的。")
    else:
        st.subheader("持仓明细（按symbol聚合）")
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
finally:
    conn.close()

st.divider()
st.markdown(
    "**导航**：「持仓与建议」录入/管理持仓并查看建议卡片 · 「掘金扫描」查看全市场模型排名 · "
    "「风险仪表盘」组合风险指标 · 「AI解释」用DeepSeek把结构化信号翻译成人话（需配置API Key）。\n\n"
    "详细方法论与验收记录见项目根目录 `docs/phase*-acceptance-report.md`。"
)
