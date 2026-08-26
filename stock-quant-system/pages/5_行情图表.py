"""
行情图表——K线 + 持仓成本/止盈止损参考线，直接复用 `daily_quotes`（前复权）
和 `risk/portfolio_risk.py::load_open_positions()`，不重新实现取价逻辑。

**为什么加这一页**：可用性测试反馈"缺少更直观易懂的功能"——建议卡片给的是文字和百分比，
对大多数人来说"在图上看到现在的位置、止损止盈线画在哪"比读文字判断力更强、更不容易算错。
"""
from __future__ import annotations

import datetime as dt

import pandas as pd
import plotly.graph_objects as go
import streamlit as st
from plotly.subplots import make_subplots

from advice.advice_engine import price_levels as _compute_price_levels
from common.db import get_connection, init_schema
from common.ui_theme import apply_theme, term_help
from risk.portfolio_risk import load_open_positions

apply_theme(page_title="行情图表", page_icon="📊")
st.title("📊 行情图表")
st.caption("K线为前复权价格；若该股票在你的持仓里，会自动画出成本价、参考止损/止盈价三条虚线，方便直接对照现价判断。")

conn = get_connection()
init_schema(conn)

col1, col2 = st.columns([3, 1])
symbol = col1.text_input("股票代码（不带交易所后缀，如 000001）", value="")
lookback = col2.selectbox("显示最近多少个交易日", [60, 120, 250, 500], index=1)

if symbol:
    symbol = symbol.strip()
    try:
        info = conn.execute("SELECT symbol, name FROM universe WHERE symbol = ?", [symbol]).df()
        name = info["name"].iloc[0] if not info.empty else None

        quotes = conn.execute(
            """
            SELECT trade_date, open, high, low, close, volume, pct_change
            FROM daily_quotes
            WHERE symbol = ? AND adjust = 'qfq'
            ORDER BY trade_date DESC
            LIMIT ?
            """,
            [symbol, int(lookback)],
        ).df().sort_values("trade_date")

        if quotes.empty:
            st.warning("没有查到这只股票的行情数据，请检查代码是否正确（不带sh/sz前缀）。")
        else:
            quotes["trade_date"] = pd.to_datetime(quotes["trade_date"])
            last_row = quotes.iloc[-1]
            st.subheader(f"{symbol} {name or ''}")

            m1, m2, m3 = st.columns(3)
            m1.metric("最新收盘价", f"¥{last_row['close']:.2f}")
            m2.metric("最新涨跌幅", f"{last_row['pct_change']:.2f}%" if pd.notna(last_row["pct_change"]) else "—")
            m3.metric("数据截止日", last_row["trade_date"].strftime("%Y-%m-%d"))

            positions = load_open_positions(conn)
            my_lots = positions[positions["symbol"] == symbol] if not positions.empty else positions
            cost_price = None
            if my_lots is not None and not my_lots.empty:
                total_shares = my_lots["shares"].sum()
                cost_price = float((my_lots["shares"] * my_lots["cost_price"]).sum() / total_shares) if total_shares else None

            fig = make_subplots(
                rows=2, cols=1, shared_xaxes=True, row_heights=[0.75, 0.25], vertical_spacing=0.03,
                subplot_titles=("价格", "成交量（手）"),
            )
            fig.add_trace(
                go.Candlestick(
                    x=quotes["trade_date"], open=quotes["open"], high=quotes["high"],
                    low=quotes["low"], close=quotes["close"],
                    increasing_line_color="#C62828", decreasing_line_color="#2E7D32",
                    name="K线",
                ),
                row=1, col=1,
            )
            vol_colors = ["#C62828" if c >= o else "#2E7D32" for o, c in zip(quotes["open"], quotes["close"])]
            fig.add_trace(
                go.Bar(x=quotes["trade_date"], y=quotes["volume"], marker_color=vol_colors, name="成交量"),
                row=2, col=1,
            )

            if cost_price is not None:
                levels = _compute_price_levels(float(last_row["close"]), cost_price)
                for key, label, color in [
                    ("cost_price", f"持仓成本 ¥{cost_price:.2f}", "#1565C0"),
                    ("stop_loss_price", f"参考止损价 ¥{levels['stop_loss_price']:.2f}", "#C62828"),
                    ("take_profit_price", f"参考止盈价 ¥{levels['take_profit_price']:.2f}", "#2E7D32"),
                ]:
                    fig.add_hline(
                        y=levels[key], line_dash="dash", line_color=color,
                        annotation_text=label, annotation_position="top left",
                        row=1, col=1,
                    )
                st.caption("虚线按你在「持仓与建议」录入的加权平均成本价 + 8%止盈止损阈值计算（与建议卡片同一套阈值）。")
            else:
                st.caption("未在「持仓与建议」页录入该股票的持仓，暂不展示成本/止盈止损参考线。")

            fig.update_layout(height=650, xaxis_rangeslider_visible=False, showlegend=False,
                               margin=dict(t=40, b=10, l=10, r=10))
            st.plotly_chart(fig, use_container_width=True)

            help_col1, help_col2 = st.columns(2)
            with help_col1:
                term_help("止盈止损")
            with help_col2:
                term_help("因子", custom_text="K线只反映价格走势本身，模型打分/因子分析请前往「掘金扫描」「AI解释」页面查看。")
    finally:
        conn.close()
else:
    st.info("请输入股票代码查看K线图。")
    conn.close()
