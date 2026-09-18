"""组合风险仪表盘——复用 `risk/portfolio_risk.py`，不重新实现风险计算。"""
from __future__ import annotations

import datetime as dt

import pandas as pd
import plotly.express as px
import streamlit as st

from common.ui_theme import apply_theme, connect_warehouse, close_warehouse, section_header, term_help
from risk.portfolio_risk import _canon_symbol, generate_risk_report


def _row_label(row: pd.Series) -> str:
    symbol = _canon_symbol(row.get("symbol"))
    name = row.get("name")
    if pd.notna(name) and str(name).strip():
        return f"{symbol} {name}"
    return symbol


def _concentration_bar_figure(conc_df: pd.DataFrame):
    df = conc_df.copy()
    df["y_label"] = df.apply(_row_label, axis=1)
    df = df.sort_values("weight_pct", ascending=True)
    df["是否超限"] = ["超过15%" if w > 0.15 else "未超限" for w in df["weight_pct"]]
    fig = px.bar(
        df,
        x="weight_pct",
        y="y_label",
        orientation="h",
        color="是否超限",
        color_discrete_map={"超过15%": "#C62828", "未超限": "#1B5E9E"},
        text=df["weight_pct"].map(lambda v: f"{v:.1%}"),
        labels={"weight_pct": "占全部持仓市值的比例", "y_label": ""},
        title="每只股票占你全部持仓市值的比例（条越长越集中）",
    )
    fig.update_traces(textposition="outside", cliponaxis=False)
    xmax = max(0.22, float(df["weight_pct"].max()) * 1.35)
    fig.update_layout(
        xaxis=dict(tickformat=".0%", range=[0, xmax]),
        yaxis=dict(
            type="category",
            categoryorder="array",
            categoryarray=df["y_label"].tolist(),
            title="",
        ),
        legend_title_text="",
        height=max(280, 48 * len(df) + 90),
        margin=dict(l=16, r=72, t=56, b=48),
    )
    fig.add_vline(x=0.15, line_dash="dash", line_color="#C62828", annotation_text="15%上限")
    return fig


def _concentration_table(conc_df: pd.DataFrame) -> pd.DataFrame:
    df = conc_df.copy()
    df["股票"] = df.apply(_row_label, axis=1)
    df = df.sort_values("weight_pct", ascending=False)
    return pd.DataFrame({
        "股票": df["股票"],
        "市值": df["market_value"].map(lambda v: f"¥{v:,.0f}" if pd.notna(v) else "—"),
        "占全部持仓": df["weight_pct"].map(lambda v: f"{v:.1%}"),
        "是否超限": ["是，建议减仓分散" if w > 0.15 else "否" for w in df["weight_pct"]],
    })

apply_theme(page_title="风险仪表盘", page_icon="⚠️")
st.title("⚠️ 组合风险仪表盘")
st.caption(
    "风险价值与条件风险价值采用历史模拟法（过去约一年真实日收益的经验分位数）。"
    "回撤按「当前持仓权重重放历史」估算，用于观察集中持仓可能经历的回落幅度。"
)

conn = connect_warehouse()
today = dt.date.today().strftime("%Y-%m-%d")
report = generate_risk_report(conn, today)

if report.get("status") != "ok":
    st.info(report.get("status", "无数据"))
else:
    if report["concentration_warning"]:
        st.error(f"⚠️ 单票集中度已达 {report['max_single_weight']:.1%}，超过15%上限，建议减仓分散。")

    col1, col2, col3, col4 = st.columns([1, 1, 1, 0.6])
    col1.metric("持仓股票数", report["n_positions"])
    col2.metric("最大单票占比", f"{report['max_single_weight']:.1%}" if report["max_single_weight"] else "—")
    var = report["portfolio_var"]
    col3.metric("组合 1 日风险价值(95%)", f"{var['var_1d_pct']:.2%}" if var.get("status") == "ok" else var.get("status", "—"))
    with col4:
        st.write("")
        term_help("风险价值")

    st.divider()
    section_header("持仓集中度", help_term="集中度", level=2)
    conc_df = pd.DataFrame(report["concentration"])
    if not conc_df.empty:
        # 用水平柱图而不是饼图：设计理论库《03_信息图示与数据墨水》引Tufte/Few的规则
        # ——饼图角度差不容易精确比较，超过4片建议一律改水平柱；持仓通常不止4只，
        # 直接统一用柱图，长度比角度更容易读出"谁比谁多多少"。
        # y 轴必须 type=category：6 位代码在 Plotly 里会被当成数字，纵坐标变成「60万」。
        fig = _concentration_bar_figure(conc_df)
        st.caption(
            "每根横条 = 这只股票市值 ÷ 你全部持仓市值之和。条越长越集中；"
            "越过红色虚线（15%）就视为单票过重。现金未计入。"
        )
        st.plotly_chart(fig, use_container_width=True)
        table = _concentration_table(conc_df)
        st.dataframe(table, use_container_width=True, hide_index=True)

    st.divider()
    st.subheader("逐股票波动率 / 风险价值")
    vol_df = pd.DataFrame(report["volatility_var_by_symbol"])
    if not vol_df.empty:
        if "symbol" in vol_df.columns:
            vol_df["symbol"] = vol_df["symbol"].map(_canon_symbol)
        st.dataframe(vol_df, use_container_width=True, hide_index=True)

    st.divider()
    st.subheader("持仓相关性矩阵")
    corr = report.get("correlation_matrix") or {}
    if corr:
        corr_df = pd.DataFrame(corr)
        corr_df.index = [_canon_symbol(i) for i in corr_df.index]
        corr_df.columns = [_canon_symbol(c) for c in corr_df.columns]
        fig2 = px.imshow(corr_df, text_auto=True, color_continuous_scale="RdBu_r", zmin=-1, zmax=1,
                          title="持仓股票两两日收益相关系数")
        fig2.update_xaxes(type="category")
        fig2.update_yaxes(type="category")
        st.plotly_chart(fig2, use_container_width=True)
        high_corr_pairs = []
        for i, s1 in enumerate(corr_df.columns):
            for s2 in corr_df.columns[i + 1:]:
                v = corr_df.loc[s1, s2]
                if pd.notna(v) and v > 0.7:
                    high_corr_pairs.append((s1, s2, v))
        if high_corr_pairs:
            st.warning("以下持仓两两高度相关（大于 0.7），分散效果可能有限：\n" +
                       "\n".join(f"- {a} 与 {b}: {v:.2f}" for a, b, v in high_corr_pairs))
    else:
        st.caption("持仓不足 2 只或价格历史不足，暂无法计算相关性矩阵。")

    st.divider()
    section_header("组合层面风险价值 / 回撤（历史模拟法）", help_term="最大回撤", level=2)
    dd = report["drawdown_since_current_weights"]
    if dd.get("status") == "ok":
        c1, c2 = st.columns(2)
        c1.metric("假设性最大回撤", f"{dd['max_drawdown']:.1%}")
        c2.metric("假设性当前回撤", f"{dd['current_drawdown']:.1%}")
    else:
        st.caption(dd.get("status", ""))

close_warehouse(conn)
