"""LLM解释层——把结构化信号 + 最新新闻标题翻译成人话，不做预测/建议本身。
未配置 `DASHSCOPE_API_KEY`（通义千问）时优雅降级为仅展示结构化数据。"""
from __future__ import annotations

import datetime as dt

import streamlit as st

from common.db import get_connection, init_schema
from common.ui_theme import apply_theme
from llm.explain_assistant import LLMNotConfiguredError, build_context_text, generate_explanation
from llm.news_fetch import fetch_stock_news

apply_theme(page_title="AI解释", page_icon="🤖")
st.title("🤖 AI解释助手")
st.caption(
    "本页只做\"把已算好的结果翻译成人话\"，不用LLM生成预测或买卖建议——所有量化结论都来自"
    "模型/因子/风险/行为金融模块的真实计算。默认使用通义千问（阿里云百炼），需在项目根目录 `.env` "
    "配置 `DASHSCOPE_API_KEY`；未配置时仍可查看原始结构化数据。"
)

symbol = st.text_input("股票代码（不带交易所后缀，如 000001）", value="")

if symbol:
    conn = get_connection()
    init_schema(conn)
    try:
        today = dt.date.today().strftime("%Y-%m-%d")
        row = conn.execute(
            "SELECT symbol, name FROM universe WHERE symbol = ?", [symbol.strip()]
        ).df()
        name = row["name"].iloc[0] if not row.empty else None

        latest_score = conn.execute(
            """
            SELECT pred_score, rank FROM prediction_log
            WHERE symbol = ? ORDER BY trade_date DESC LIMIT 1
            """,
            [symbol.strip()],
        ).df()
        pred_score = latest_score["pred_score"].iloc[0] if not latest_score.empty else None
        rank = int(latest_score["rank"].iloc[0]) if not latest_score.empty else None

        factor_row = conn.execute(
            """
            SELECT factor_mom_12_1, factor_roe, factor_bp, factor_gross_margin
            FROM (SELECT * FROM read_parquet('data/feature_panel.parquet')
                  WHERE symbol = ? ORDER BY trade_date DESC LIMIT 1)
            """,
            [symbol.strip()],
        ).df() if __import__("pathlib").Path("data/feature_panel.parquet").exists() else None
        factor_snapshot = factor_row.iloc[0].to_dict() if factor_row is not None and not factor_row.empty else None

        from behavior.chip_distribution import compute_disposition_proxy
        disp = compute_disposition_proxy(conn, [symbol.strip()], today)
        behavior_flags = {"disposition_flag": disp["disposition_flag"].iloc[0]} if not disp.empty else None
    finally:
        conn.close()

    st.subheader(f"{symbol} {name or ''}")
    with st.spinner("正在抓取最新新闻..."):
        news_df = fetch_stock_news(symbol.strip())
    news_titles = news_df["title"].tolist() if not news_df.empty else []

    context_text = build_context_text(
        symbol=symbol.strip(), name=name, pred_score=pred_score, rank=rank,
        factor_snapshot=factor_snapshot, behavior_flags=behavior_flags,
        risk_flags=None, news_titles=news_titles,
    )

    col1, col2 = st.columns(2)
    with col1:
        st.markdown("#### 结构化数据（真实计算结果）")
        st.text(context_text)
    with col2:
        st.markdown("#### AI解释")
        try:
            with st.spinner("调用通义千问生成解释..."):
                explanation = generate_explanation(context_text)
            st.write(explanation)
        except LLMNotConfiguredError as e:
            st.warning(str(e))
        except Exception as e:
            st.error(f"调用失败：{e}")

    if news_titles:
        st.divider()
        st.markdown("#### 最新相关新闻")
        for _, n in news_df.head(8).iterrows():
            st.markdown(f"- [{n['title']}]({n['url']}) —— {n['source']} {n['publish_time']}")
else:
    st.info("请输入股票代码。")
