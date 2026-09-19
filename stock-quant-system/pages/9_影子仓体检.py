"""影子仓体检——机器多策略纸面账户的日更汇总查看 / 导出 / AI 解读。

短窗盈亏仅供监控，不构成投资建议，不进入冠军晋升。
每周新建 8 户队列，账户至少留存 60 天（同时活跃约 ≤64 户；约 30 天内可累积到 ~40 户）。
"""
from __future__ import annotations

import datetime as dt
import json

import pandas as pd
import streamlit as st

from advice.shadow_farm import (
    HORIZONS,
    MAX_CONCURRENT_ACCOUNTS,
    RETENTION_DAYS,
    STRATEGY_SPECS,
    load_latest_shadow_report,
    report_to_markdown,
    run_shadow_farm_once,
    shadow_nav_frame,
)
from common.db import is_read_only
from common.ui_theme import apply_theme, close_warehouse, connect_warehouse, render_production_banners
from llm.explain_assistant import LLMNotConfiguredError, build_shadow_farm_context, generate_explanation

apply_theme(page_title="影子仓体检", page_icon="🧪")
st.title("🧪 影子仓体检")
st.caption(
    f"每周新建 {len(STRATEGY_SPECS)} 套策略账户（ISO 周队列），"
    f"账户一经建立至少留存 {RETENTION_DAYS} 天（同时活跃约 ≤{MAX_CONCURRENT_ACCOUNTS} 户）；"
    f"日更盯市全部未过期账户，汇总 {'/'.join(str(h) for h in HORIZONS)} 日净值，"
    "并写入 `data/shadow_farm/accounts/<账户>/`（CSV+JSON）与 `panel_latest.csv`。"
    "周六计划任务会在周末灌库成功后自动生成双池建议并开本周队列换票。"
    "这是监控体检，不是练手账户，也**不会**据此自动改冠军模型。"
)

conn = connect_warehouse()
readonly = is_read_only(conn)
try:
    render_production_banners(conn)

    with st.expander("8 套策略是什么？", expanded=False):
        st.dataframe(
            pd.DataFrame(
                [
                    {
                        "strategy_id": s.strategy_id,
                        "说明": s.label,
                        "池": s.pool_id,
                        "模式": s.mode,
                        "top_n": s.top_n,
                        "资金比例": s.deploy_pct,
                    }
                    for s in STRATEGY_SPECS
                ]
            ),
            use_container_width=True,
            hide_index=True,
        )

    c1, c2 = st.columns(2)
    with c1:
        if st.button("立即跑一批影子仓", type="primary", disabled=readonly):
            with st.spinner("正在确保本周队列并盯市…"):
                # 手动：允许对本周队列换票（等同周末）
                report = run_shadow_farm_once(open_new_cohort=True)
                st.session_state["shadow_report"] = report
                st.success(
                    f"完成：{report.get('status')} · cohort={report.get('cohort_id')} · "
                    f"as_of={report.get('as_of')} · 留存账户={report.get('active_accounts')}"
                )
                st.rerun()
    with c2:
        st.caption(
            "日更任务：`StockQuant-ShadowFarm`（每天 03:00，盯市全部未满 60 天账户）。"
            "换票任务：`StockQuant-WeekendResearch`（周六 09:00，灌库→双池建议→新建本周 8 户）。"
        )

    report = st.session_state.get("shadow_report") or load_latest_shadow_report(conn)
    if not report:
        st.info("还没有影子仓报告。可点上方「立即跑一批」，或等凌晨计划任务。")
        st.stop()

    m1, m2, m3, m4 = st.columns(4)
    m1.metric("最近状态", str(report.get("status") or "—"))
    m2.metric("净值日 as_of", str(report.get("as_of") or "—"))
    m3.metric("本周队列", str(report.get("cohort_id") or "—"))
    m4.metric(
        "留存账户",
        f"{report.get('active_accounts') or len(report.get('strategies') or [])}"
        f" / {report.get('active_cohorts') or '—'} 周",
    )
    st.write(report.get("message") or "")
    st.caption(report.get("disclaimer") or "")
    exp = report.get("monitoring_export") or {}
    if exp:
        st.caption(
            f"监测落盘：已导出 {exp.get('exported_accounts')} 户 → "
            f"`{exp.get('accounts_dir')}`；横截面 `{exp.get('panel_csv')}`"
        )

    rows = report.get("strategies") or []
    if rows:
        wide = []
        for r in rows:
            hz = r.get("horizons") or {}
            item = {
                "队列": r.get("cohort_id"),
                "账户": r.get("account_id"),
                "策略": r.get("strategy_id"),
                "说明": r.get("label"),
                "池": r.get("pool_id"),
                "净值": r.get("nav_cny"),
                "现金": r.get("cash_cny"),
                "市值": r.get("market_value_cny"),
                "成交": r.get("filled"),
                "待开盘": r.get("pending"),
                "拒绝": r.get("rejected"),
                "备注": r.get("note"),
            }
            for h in HORIZONS:
                item[f"ret_{h}d"] = hz.get(f"ret_{h}d")
            wide.append(item)
        df = pd.DataFrame(wide)
        st.subheader("本周队列各策略对比")
        base_cols = [
            "队列",
            "账户",
            "策略",
            "说明",
            "池",
            "净值",
            "现金",
            "市值",
            "成交",
            "待开盘",
            "拒绝",
            "备注",
        ]
        st.dataframe(
            df[[c for c in base_cols if c in df.columns]],
            use_container_width=True,
            hide_index=True,
        )
        ret_cols = ["策略"] + [f"ret_{h}d" for h in HORIZONS]
        st.caption("相对已有净值快照的回溯收益（列齐全：含 20/30 日）")
        st.dataframe(
            df[[c for c in ret_cols if c in df.columns]],
            use_container_width=True,
            hide_index=True,
        )

        md = report_to_markdown(report)
        json_text = json.dumps(report, ensure_ascii=False, indent=2, default=str)
        csv_text = df.to_csv(index=False)

        e1, e2, e3 = st.columns(3)
        e1.download_button(
            "导出 CSV",
            data=csv_text.encode("utf-8-sig"),
            file_name=f"shadow_farm_{report.get('as_of') or 'latest'}.csv",
            mime="text/csv",
        )
        e2.download_button(
            "导出 JSON",
            data=json_text.encode("utf-8"),
            file_name=f"shadow_farm_{report.get('as_of') or 'latest'}.json",
            mime="application/json",
        )
        e3.download_button(
            "导出 Markdown",
            data=md.encode("utf-8"),
            file_name=f"shadow_farm_{report.get('as_of') or 'latest'}.md",
            mime="text/markdown",
        )

        st.subheader("AI 解读（可选）")
        st.caption("只翻译上表数字；未配置通义密钥时可复制下方 Markdown 到其他大模型。")
        if st.button("用 AI 解读本次汇总"):
            ctx = build_shadow_farm_context(report, as_of=dt.date.today().isoformat())
            try:
                with st.spinner("正在调用大模型…"):
                    text = generate_explanation(ctx, as_of=dt.date.today().isoformat())
                st.session_state["shadow_llm_text"] = text
            except LLMNotConfiguredError as exc:
                st.warning(str(exc))
                st.session_state["shadow_llm_text"] = None
            except Exception as exc:  # noqa: BLE001
                st.error(f"解读失败：{exc}")

        if st.session_state.get("shadow_llm_text"):
            st.markdown(st.session_state["shadow_llm_text"])

        with st.expander("可复制给大模型的 Markdown 摘要", expanded=False):
            st.code(md, language="markdown")

    st.subheader("历史净值快照（库内 · 含往周留存账户）")
    hist = shadow_nav_frame(conn)
    if hist.empty:
        st.caption("尚无 shadow_nav_daily 记录。")
    else:
        st.dataframe(hist.head(200), use_container_width=True, hide_index=True)

finally:
    close_warehouse(conn)
