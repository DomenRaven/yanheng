"""组合风险仪表盘——复用 `risk/portfolio_risk.py`，不重新实现风险计算。"""
from __future__ import annotations

import datetime as dt

import pandas as pd
import plotly.express as px
import streamlit as st

from common.db import get_connection, init_schema
from risk.portfolio_risk import generate_risk_report

st.set_page_config(page_title="风险仪表盘", page_icon="⚠️", layout="wide")
st.title("⚠️ 组合风险仪表盘")
st.caption(
    "VaR/CVaR 用历史模拟法（过去约1年真实收益的经验分位数），回撤是\"按当前持仓权重重放历史\"的"
    "假设性回撤，不是真实调仓后的回撤——方法边界见 `risk/portfolio_risk.py` 模块docstring。"
)

conn = get_connection()
init_schema(conn)
try:
    today = dt.date.today().strftime("%Y-%m-%d")
    report = generate_risk_report(conn, today)
finally:
    conn.close()

if report.get("status") != "ok":
    st.info(report.get("status", "无数据"))
else:
    if report["concentration_warning"]:
        st.error(f"⚠️ 单票集中度已达 {report['max_single_weight']:.1%}，超过15%上限，建议减仓分散。")

    col1, col2, col3 = st.columns(3)
    col1.metric("持仓股票数", report["n_positions"])
    col2.metric("最大单票占比", f"{report['max_single_weight']:.1%}" if report["max_single_weight"] else "—")
    var = report["portfolio_var"]
    col3.metric("组合1日VaR(95%)", f"{var['var_1d_pct']:.2%}" if var.get("status") == "ok" else var.get("status", "—"))

    st.divider()
    st.subheader("持仓集中度")
    conc_df = pd.DataFrame(report["concentration"])
    if not conc_df.empty:
        fig = px.pie(conc_df, values="market_value", names="symbol", title="持仓市值占比")
        st.plotly_chart(fig, use_container_width=True)

    st.divider()
    st.subheader("逐股票波动率 / VaR")
    vol_df = pd.DataFrame(report["volatility_var_by_symbol"])
    if not vol_df.empty:
        st.dataframe(vol_df, use_container_width=True, hide_index=True)

    st.divider()
    st.subheader("持仓相关性矩阵")
    corr = report.get("correlation_matrix") or {}
    if corr:
        corr_df = pd.DataFrame(corr)
        fig2 = px.imshow(corr_df, text_auto=True, color_continuous_scale="RdBu_r", zmin=-1, zmax=1,
                          title="持仓股票两两日收益相关系数")
        st.plotly_chart(fig2, use_container_width=True)
        high_corr_pairs = []
        for i, s1 in enumerate(corr_df.columns):
            for s2 in corr_df.columns[i + 1:]:
                v = corr_df.loc[s1, s2]
                if pd.notna(v) and v > 0.7:
                    high_corr_pairs.append((s1, s2, v))
        if high_corr_pairs:
            st.warning("以下持仓两两高度相关（>0.7），\"分散持仓\"的实际效果有限：\n" +
                       "\n".join(f"- {a} vs {b}: {v:.2f}" for a, b, v in high_corr_pairs))
    else:
        st.caption("持仓不足2只或价格历史不足，暂无法计算相关性矩阵。")

    st.divider()
    st.subheader("组合层面VaR / 回撤（历史模拟法）")
    dd = report["drawdown_since_current_weights"]
    if dd.get("status") == "ok":
        c1, c2 = st.columns(2)
        c1.metric("假设性最大回撤", f"{dd['max_drawdown']:.1%}")
        c2.metric("假设性当前回撤", f"{dd['current_drawdown']:.1%}")
    else:
        st.caption(dd.get("status", ""))
