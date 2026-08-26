"""全市场掘金扫描——复用 `advice/scanner.py::run_scan()`，不重新实现排序逻辑。"""
from __future__ import annotations

import streamlit as st

st.set_page_config(page_title="掘金扫描", page_icon="🔍", layout="wide")
st.title("🔍 全市场掘金扫描")
st.caption("模型打分是相对排序，不是收益预测；点击下方按钮运行一次最新扫描（用生产冠军模型）。")

top_n = st.slider("显示前 N 名", min_value=10, max_value=200, value=50, step=10)

if st.button("🔄 运行最新扫描", type="primary"):
    with st.spinner("正在加载冠军模型 + 计算全市场快照 + 打分..."):
        from advice.scanner import run_scan
        full_ranked, top_pick, trade_date = run_scan(top_n=top_n)
        st.session_state["scan_result"] = (full_ranked, top_pick, str(trade_date))

cached = st.session_state.get("scan_result")
if cached:
    full_ranked, top_pick, trade_date = cached
    st.success(f"快照交易日：{trade_date}，全市场候选：{len(full_ranked)} 只，"
               f"其中可交易：{int(full_ranked['is_tradable'].sum())} 只")

    tab1, tab2 = st.tabs(["Top候选（含行为金融冲突提示）", "全量排名"])
    with tab1:
        show_cols = ["rank", "symbol", "name", "exchange", "pred_score", "close",
                     "factor_roe", "factor_mom_12_1", "conflict_flag"]
        show_cols = [c for c in show_cols if c in top_pick.columns]
        st.dataframe(top_pick[show_cols], use_container_width=True, hide_index=True)
    with tab2:
        st.dataframe(full_ranked, use_container_width=True, hide_index=True, height=600)
else:
    st.info("点击上方按钮运行扫描。")
