"""全市场掘金扫描——复用 `advice/scanner.py::run_scan()`，不重新实现排序逻辑。"""
from __future__ import annotations

import streamlit as st

from common.ui_theme import apply_theme, term_help

apply_theme(page_title="掘金扫描", page_icon="🔍")
st.title("🔍 全市场掘金扫描")
col_cap, col_help1, col_help2 = st.columns([6, 1, 1])
col_cap.caption("模型打分是相对排序，不是收益预测；点击下方按钮运行一次最新扫描（用生产冠军模型）。")
with col_help1:
    term_help("RankIC")
with col_help2:
    term_help("因子")

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
    _rename = {
        "rank": "排名", "symbol": "代码", "name": "名称", "exchange": "交易所",
        "pred_score": "模型打分", "close": "最新价", "factor_roe": "ROE因子",
        "factor_mom_12_1": "动量因子", "conflict_flag": "行为金融提示",
    }
    with tab1:
        show_cols = ["rank", "symbol", "name", "exchange", "pred_score", "close",
                     "factor_roe", "factor_mom_12_1", "conflict_flag"]
        show_cols = [c for c in show_cols if c in top_pick.columns]
        st.dataframe(
            top_pick[show_cols].rename(columns=_rename),
            use_container_width=True, hide_index=True,
        )
        st.caption("「模型打分」只是全市场相对排序用的分数，本身没有单位、也不是预期收益率——分数越高排名越靠前。")
    with tab2:
        st.dataframe(full_ranked.rename(columns=_rename), use_container_width=True, hide_index=True, height=600)
else:
    st.info("点击上方按钮运行扫描。")
