"""LLM解释层——把结构化信号 + 最新新闻标题翻译成人话，不做预测/建议本身。
未配置 `DASHSCOPE_API_KEY`（通义千问）时优雅降级为仅展示结构化数据。
"""
from __future__ import annotations

import datetime as dt
from pathlib import Path

import streamlit as st

from common.symbol_lookup import render_symbol_picker
from common.ui_theme import apply_theme, connect_warehouse
from llm.explain_assistant import LLMNotConfiguredError, build_context_text, generate_explanation
from llm.news_fetch import fetch_stock_news

apply_theme(page_title="AI解释", page_icon="🤖")
st.title("🤖 AI解释助手")
st.caption(
    "本页只做\"把已算好的结果翻译成人话\"，不用LLM生成预测或买卖建议——所有量化结论都来自"
    "模型/因子/风险/行为金融模块的真实计算。通义/DeepSeek 都没有自己的实时日历；"
    "「今天」以本机日期为准，新闻来自东方财富即时检索。需在项目根目录 `.env` "
    "配置 `DASHSCOPE_API_KEY`；未配置时仍可查看原始结构化数据。"
)
st.caption(f"本机日历：{dt.date.today().isoformat()}（写入提示词的「今天」）。模型训练记忆停在旧年份是正常现象，不是断网。")

conn = connect_warehouse()
symbol = render_symbol_picker(conn, key="ai_symbol")

if not symbol:
    st.info("请输入代码或公司名称。")
    conn.close()
    st.stop()

try:
    today = dt.date.today().strftime("%Y-%m-%d")
    code = symbol.strip()
    row = conn.execute(
        "SELECT symbol, name FROM universe WHERE symbol = ?", [code]
    ).df()
    name = row["name"].iloc[0] if not row.empty else None

    latest_score = conn.execute(
        """
        SELECT pred_score, rank, trade_date FROM prediction_log
        WHERE symbol = ? ORDER BY trade_date DESC LIMIT 1
        """,
        [code],
    ).df()
    pred_score = latest_score["pred_score"].iloc[0] if not latest_score.empty else None
    rank = int(latest_score["rank"].iloc[0]) if not latest_score.empty else None
    snapshot_trade_date = None
    if not latest_score.empty and "trade_date" in latest_score.columns:
        snapshot_trade_date = str(latest_score["trade_date"].iloc[0])[:10]

    factor_row = None
    panel_path = Path("data/feature_panel.parquet")
    if panel_path.exists():
        factor_row = conn.execute(
            """
            SELECT factor_mom_12_1, factor_roe, factor_bp, factor_gross_margin
            FROM (SELECT * FROM read_parquet('data/feature_panel.parquet')
                  WHERE symbol = ? ORDER BY trade_date DESC LIMIT 1)
            """,
            [code],
        ).df()
    factor_snapshot = factor_row.iloc[0].to_dict() if factor_row is not None and not factor_row.empty else None

    from behavior.chip_distribution import compute_disposition_proxy

    disp = compute_disposition_proxy(conn, [code], today)
    behavior_flags = {"disposition_flag": disp["disposition_flag"].iloc[0]} if not disp.empty else None
finally:
    conn.close()

st.subheader(f"{symbol} {name or ''}")

# 先做完网络调用再画两列。不要用 st.spinner 包 st.columns：
# Streamlit 1.53+ 的 TransientNode 在 spinner 结束拆节点时，会触发
# React removeChild（「被移除的节点不是该节点的子节点」）。
news_df = fetch_stock_news(code)
news_items = []
if not news_df.empty:
    for _, n in news_df.head(8).iterrows():
        news_items.append({
            "title": str(n["title"]),
            "publish_time": str(n["publish_time"]) if "publish_time" in n.index else "时间未知",
        })
news_titles = [it["title"] for it in news_items]
context_text = build_context_text(
    symbol=code,
    name=name,
    pred_score=pred_score,
    rank=rank,
    factor_snapshot=factor_snapshot,
    behavior_flags=behavior_flags,
    risk_flags=None,
    as_of=today,
    snapshot_trade_date=snapshot_trade_date,
    news_items=news_items,
)

explanation = None
llm_warn = None
llm_err = None
try:
    explanation = generate_explanation(context_text, as_of=today)
except LLMNotConfiguredError as exc:
    llm_warn = str(exc)
except Exception as exc:  # noqa: BLE001 - 展示给用户，不能让整页红屏
    llm_err = f"调用失败：{exc}"

col1, col2 = st.columns(2)
with col1:
    st.markdown("#### 结构化数据（真实计算结果）")
    st.text(context_text)
with col2:
    st.markdown("#### AI解释")
    if explanation:
        st.markdown(explanation)
    elif llm_warn:
        st.warning(llm_warn)
    else:
        st.error(llm_err or "解释生成失败")

if news_titles:
    st.divider()
    st.markdown("#### 最新相关新闻")
    for _, n in news_df.head(8).iterrows():
        title = str(n["title"]).replace("[", "［").replace("]", "］")
        extra = f"{n['source']} {n['publish_time']}"
        st.markdown(f"- [{title}]({n['url']}) —— {extra}")
