"""名词解释——集中展示 `common/ui_theme.py` 里维护的术语表，与各页面「❓」气泡
同一份数据源，不重复维护两份解释文案（对齐vibe coding八荣八耻第4条）。"""
from __future__ import annotations

import streamlit as st

from common.ui_theme import apply_theme, close_warehouse, connect_warehouse, glossary_dict

apply_theme(page_title="名词解释", page_icon="📖")
st.title("📖 名词解释")
st.caption("本页汇总系统里出现的专业术语的大白话解释；各页面关键指标旁的「❓」按钮也是同一份内容，点开即看，不用跳页。")

keyword = st.text_input("搜索术语（留空显示全部）", value="")

glossary = glossary_dict()
for term, definition in glossary.items():
    if keyword and keyword.strip() not in term:
        continue
    with st.container(border=True):
        st.markdown(f"#### {term}")
        st.write(definition)

st.divider()
st.info(
    "更完整的操作说明见：`docs/manuals/用户使用说明书.html`。"
)

_conn = connect_warehouse()
close_warehouse(_conn)
