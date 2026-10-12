"""全市场掘金扫描——复用 `advice/scanner.py::run_scan()`，沪深/北交所分池。"""
from __future__ import annotations

from pathlib import Path

import streamlit as st

from advice.champion_registry import describe_champion
from advice.model_selection import get_active_hs_choice, list_hs_choices
from common.ui_theme import (
    apply_theme,
    close_warehouse,
    connect_warehouse,
    render_hs_model_switch,
    require_warehouse_for_decisions,
    term_help,
)

apply_theme(page_title="掘金扫描", page_icon="🔍")
st.title("🔍 掘金扫描（分池）")

conn = connect_warehouse()
require_warehouse_for_decisions(conn)

col_cap, col_help1, col_help2 = st.columns([6, 1, 1])
col_cap.caption(
    "沪深池用侧栏或下方按钮选中的打分模型排序，北交所池只用北交所冠军，两池不混排。"
    "切换扫描池或打分模型后，请重新运行扫描。"
)
with col_help1:
    term_help("分池扫描")
with col_help2:
    term_help("坚持度")

pool_label = st.radio(
    "扫描池",
    ["hs", "bj"],
    format_func=lambda x: "沪深池" if x == "hs" else "北交所池",
    horizontal=True,
    key="scan_pool",
)
if pool_label == "hs":
    render_hs_model_switch("scan")
    _choice = get_active_hs_choice()
    _item = next((c for c in list_hs_choices() if c["choice_id"] == _choice), None)
    if _item and _item["ready"]:
        st.info(
            f"本次沪深扫描使用 **{_item['label']}** `{_item['run_id']}` · "
            f"样本外 RankIC `{_item['rank_ic']}`。"
            "冠军指针不会因为这次切换而改变。"
        )
    else:
        st.warning("沪深打分模型未就绪，无法扫描。")
else:
    _champ = describe_champion(pool_id="bj")
    if _champ["ready"]:
        st.info(
            f"将使用 **北交所冠军** `{_champ['run_id']}` · "
            f"训练面板 `{_champ['date_range']}` · "
            f"样本外 RankIC `{_champ['walk_forward_rank_ic']}`"
            "（与下方日频快照交易日可以不同）"
        )
    else:
        st.warning("北交所冠军未就绪，无法扫描。请先完成该池重训晋升。")

top_n = st.slider("显示前 N 名", min_value=10, max_value=200, value=50, step=10)

if "scan_by_pool" not in st.session_state:
    st.session_state["scan_by_pool"] = {}
# 兼容旧单键缓存
legacy = st.session_state.get("scan_result")
if legacy and isinstance(legacy, tuple):
    if len(legacy) == 4:
        st.session_state["scan_by_pool"].setdefault(legacy[3], legacy)
    else:
        st.session_state["scan_by_pool"].setdefault("hs", (*legacy, "hs"))

if st.button("🔄 运行最新扫描", type="primary"):
    with st.spinner(f"正在加载 {'沪深当前打分模型' if pool_label == 'hs' else '北交所冠军'}并打分..."):
        from advice.scanner import run_scan

        try:
            full_ranked, top_pick, trade_date = run_scan(
                top_n=top_n, pool_id=pool_label, source="ui"
            )
            payload = (full_ranked, top_pick, str(trade_date), pool_label)
            st.session_state["scan_by_pool"][pool_label] = payload
            st.session_state["scan_result"] = payload  # 兼容旧逻辑
        except FileNotFoundError as exc:
            st.error(str(exc))
            st.session_state["scan_by_pool"].pop(pool_label, None)

cached = st.session_state["scan_by_pool"].get(pool_label)
_rename = {
    "rank": "排名",
    "symbol": "代码",
    "name": "名称",
    "exchange": "交易所",
    "pred_score": "模型打分",
    "close": "最新价",
    "factor_roe": "盈利因子",
    "factor_mom_12_1": "动量因子",
    "conflict_flag": "行为冲突提示",
    "reason_one_liner": "理由（一句话）",
    "industry_code": "行业代码",
    "hits": "命中次数",
    "window": "窗口扫描数",
    "hit_ratio": "命中率",
    "avg_rank": "平均名次",
    "best_rank": "最好名次",
}

if cached:
    full_ranked, top_pick, trade_date, cached_pool = cached
    st.success(
        f"池：{'沪深' if cached_pool == 'hs' else '北交所'} · 快照交易日：{trade_date}，"
        f"候选：{len(full_ranked)} 只，其中可交易：{int(full_ranked['is_tradable'].sum())} 只"
    )
    if cached_pool == "hs" and "exchange" in full_ranked.columns:
        bj_n = int((full_ranked["exchange"] == "bj").sum())
        if bj_n:
            st.warning(f"内部异常：沪深池结果含 {bj_n} 只北交所，请报缺陷。")
    top3 = top_pick.head(3)
    if not top3.empty:
        names = [
            f"#{int(r['rank'])} {r['symbol']} {r.get('name') or ''}".strip()
            for _, r in top3.iterrows()
        ]
        st.info(
            "模型前三名：" + " · ".join(names)
            + " —— 可到「持仓与建议」切换同池后使用「一键练习」。"
        )

    tab1, tab2, tab3 = st.tabs(["前列候选（含行为冲突提示）", "全量排名", "榜单 / 坚持度"])
    with tab1:
        show_cols = [
            "rank",
            "symbol",
            "name",
            "exchange",
            "pred_score",
            "close",
            "factor_roe",
            "factor_mom_12_1",
            "conflict_flag",
            "reason_one_liner",
            "industry_code",
        ]
        show_cols = [c for c in show_cols if c in top_pick.columns]
        st.dataframe(
            top_pick[show_cols].rename(columns=_rename),
            use_container_width=True,
            hide_index=True,
        )
        st.caption(
            "「模型打分」仅用于本池相对排序。切换上方扫描池会切换本表与坚持度（各池缓存独立）。"
        )
    with tab2:
        st.dataframe(
            full_ranked.rename(columns=_rename),
            use_container_width=True,
            hide_index=True,
            height=600,
        )
    with tab3:
        from advice.scan_archive import persistence_full_attendance, persistence_hit_counts

        st.caption("坚持度按**当前所选池**统计反复进入前 50 名的次数。")
        full5 = persistence_full_attendance(conn, pool_id=pool_label, window_runs=5, top_k=50)
        st.markdown("#### 近 5 次扫描全勤前 50")
        if full5.empty:
            st.info("本池存档不足 5 次有效扫描，暂无全勤名单。")
        else:
            st.dataframe(full5.rename(columns=_rename), use_container_width=True, hide_index=True)
        hits20 = persistence_hit_counts(conn, pool_id=pool_label, lookback_runs=20, top_k=50)
        st.markdown("#### 近 20 次扫描前 50 命中次数")
        if hits20.empty:
            st.info("本池尚无扫描存档。")
        else:
            st.dataframe(hits20.head(50).rename(columns=_rename), use_container_width=True, hide_index=True)
else:
    st.info(f"当前「{'沪深' if pool_label == 'hs' else '北交所'}池」尚无缓存结果，请点击上方运行扫描。")
    from advice.scan_archive import persistence_hit_counts

    bj_ready = Path("mlops/registry/champion_bj.json").exists()
    if pool_label == "bj" and not bj_ready:
        st.warning(
            "北交所池冠军尚未晋升（缺少 champion_bj.json）。"
            "不会用沪深模型代打。请运行：python -m mlops.retrain_schedule --pool bj"
        )
    hits20 = persistence_hit_counts(conn, pool_id=pool_label, lookback_runs=20, top_k=50)
    st.markdown("### 历史坚持度（本池）")
    if hits20.empty:
        st.info("本池尚无扫描存档。")
    else:
        st.dataframe(hits20.head(50), use_container_width=True, hide_index=True)

close_warehouse(conn)
