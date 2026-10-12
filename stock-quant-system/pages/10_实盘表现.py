"""实盘表现——把历史扫描快照和建仓卡片拿出来，对照之后真实涨跌算账。

页面只负责展示：数字来自 `docs/live-performance-latest.json`，由
`python -m scripts.report_live_performance` 只读打开仓库生成；统计口径在 `research/live_performance.py`。
本页不写库。
"""
from __future__ import annotations

import json
from pathlib import Path

import pandas as pd
import streamlit as st

from common.ui_theme import apply_theme, render_trust_footer, section_header, term_help
from scripts.report_live_performance import LATEST_JSON, POOL_LABEL, generate

apply_theme(page_title="实盘表现", page_icon="📊")
st.title("📊 实盘表现")
st.caption(
    "每次掘金扫描的分数和每张建仓卡片都留了底。本页把它们和之后的真实收盘价对账："
    "模型名次和后来涨跌顺序有多一致、扣掉低价股整体上涨后还剩多少、建仓卡片比沪深300多赚多少。"
)

_RESIDUAL_HELP = (
    "这段时间低价股整体涨得多，而模型又偏好低价股，所以原始排名相关系数里混进了「低价效应」。"
    "残差排名相关系数先把股价高低从分数和收益里扣掉，再算两者名次有多一致；它才是模型自己的区分度。"
)


@st.cache_data(ttl=300, show_spinner=False)
def _load_latest(path_str: str, mtime: float) -> dict | None:
    path = Path(path_str)
    if not path.is_file():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def _pct(v):
    return None if v is None else float(v) * 100.0


c_btn, c_note = st.columns([1, 3])
with c_btn:
    if st.button("重新计算（只读）", type="primary"):
        with st.spinner("只读取数并重算…"):
            try:
                generate()
                _load_latest.clear()
                st.success("已重算并写入 docs/live-performance-latest.json")
            except Exception as exc:  # noqa: BLE001
                st.error(f"重算失败：{exc}")
with c_note:
    st.caption("重算只读取仓库，不写库；行情更新后点一次即可。命令行等价：`python -m scripts.report_live_performance`。")

mtime = LATEST_JSON.stat().st_mtime if LATEST_JSON.is_file() else 0.0
report = _load_latest(str(LATEST_JSON), mtime)
if not report:
    st.info("还没有实盘表现报告。点上方「重新计算（只读）」，或在项目目录执行下面的命令。")
    st.code("python -m scripts.report_live_performance", language="powershell")
    st.stop()

snaps = report.get("snapshots") or []
indep = report.get("independence") or {}
primary_h = report.get("primary_horizon", 10)
hs_diag = [
    s for s in snaps
    if s.get("pool_id") == "hs" and s.get("confound") and s["confound"].get("residual_rank_ic") is not None
]
latest_hs = sorted(hs_diag, key=lambda s: s["trade_date"])[-1] if hs_diag else None

m1, m2, m3, m4 = st.columns(4)
m1.metric("行情截止", str(report.get("data_end") or "—"))
m2.metric("有后续行情的快照", f"{sum(1 for s in snaps if s.get('confound'))} / {len(snaps)}")
m3.metric(f"独立 {indep.get('window_days', primary_h)} 日窗口", f"{indep.get('n_independent', '—')}（走满 {indep.get('n_complete', '—')}）")
if latest_hs:
    m4.metric(
        f"沪深最近残差 RankIC（{latest_hs['trade_date']}）",
        f"{latest_hs['confound']['residual_rank_ic']:.2f}",
        delta=f"原始 {latest_hs['confound']['raw_rank_ic']:.2f}",
        delta_color="off",
    )
else:
    m4.metric("沪深最近残差 RankIC", "—")
st.caption(f"报告生成时间 {report.get('generated_at', '—')}。")

section_header("一、解读", None, level=2)
for i, line in enumerate(report.get("interpretation") or [], 1):
    st.write(f"{i}. {line}")

section_header(f"二、快照排名相关系数（{primary_h} 日）", "排名相关系数", level=2)
st.write(
    "读法：0 表示模型名次和之后涨跌顺序没有关系，0.3 表示有中等程度的一致，1 表示完全一致。"
    "p 值越小，这个数越不像是碰巧得到的。n 是参与计算、目标日有真实行情的股票数。"
)
rows = []
for s in snaps:
    b = (s.get("by_horizon") or {}).get(str(primary_h)) or {}
    rows.append(
        {
            "池": s.get("pool_label") or POOL_LABEL.get(s.get("pool_id"), s.get("pool_id")),
            "快照日": s.get("trade_date"),
            "模型": s.get("model_run_id"),
            "可交易股票": s.get("n_tradable"),
            "后续交易日": s.get("days_after"),
            "实际天数": b.get("effective"),
            "n": b.get("n"),
            "RankIC": b.get("rank_ic"),
            "p 值": b.get("p_value"),
            "目标日行情覆盖": _pct(b.get("coverage")),
            "备注": s.get("note") or "",
        }
    )
ic_df = pd.DataFrame(rows)
st.dataframe(
    ic_df,
    use_container_width=True,
    hide_index=True,
    column_config={
        "RankIC": st.column_config.NumberColumn(format="%.3f"),
        "p 值": st.column_config.NumberColumn(format="%.3f"),
        "目标日行情覆盖": st.column_config.NumberColumn(format="%.0f%%"),
    },
)
with st.expander("各期限 RankIC（1/3/5/10/20 日）", expanded=False):
    wide = []
    for s in snaps:
        item = {"池": s.get("pool_label"), "快照日": s.get("trade_date"), "模型": s.get("model_run_id")}
        for h in report.get("horizons") or []:
            b = (s.get("by_horizon") or {}).get(str(h)) or {}
            item[f"{h}日"] = b.get("rank_ic")
            item[f"{h}日 n"] = b.get("n")
        wide.append(item)
    st.dataframe(pd.DataFrame(wide), use_container_width=True, hide_index=True)
    st.caption("期限超出数据末尾时按最后一个交易日截断，表中「实际天数」写的是真正用到的天数。")

section_header("三、扣掉低价效应后还剩多少", None, level=2)
term_help("残差排名相关系数", _RESIDUAL_HELP)
st.write(
    "ρ(价格, 收益) 为负表示这段时间低价股涨得多；ρ(名次, 价格) 为正表示模型把低价股排在前面。"
    "两者同时成立时，原始 RankIC 里有一部分来自低价效应，残差 RankIC 是扣掉它之后模型自己的那部分。"
)
diag_rows = []
for s in snaps:
    c = s.get("confound")
    if not c:
        continue
    diag_rows.append(
        {
            "池": s.get("pool_label"),
            "快照日": s.get("trade_date"),
            "模型": s.get("model_run_id"),
            "n": c.get("n"),
            "原始 RankIC": c.get("raw_rank_ic"),
            "ρ(价格, 收益)": c.get("spearman_price_fwd"),
            "ρ(名次, 价格)": c.get("spearman_rank_price"),
            "残差 RankIC": c.get("residual_rank_ic"),
            "残差 p 值": c.get("p_residual"),
            "价格五分位内 RankIC 均值": c.get("quintile_mean_rank_ic"),
        }
    )
if diag_rows:
    st.dataframe(
        pd.DataFrame(diag_rows),
        use_container_width=True,
        hide_index=True,
        column_config={
            k: st.column_config.NumberColumn(format="%.3f")
            for k in ["原始 RankIC", "ρ(价格, 收益)", "ρ(名次, 价格)", "残差 RankIC", "残差 p 值", "价格五分位内 RankIC 均值"]
        },
    )
    pick = st.selectbox(
        "看某个快照的分十档（10 = 分数最高档）",
        options=[f"{r['池']} @ {r['快照日']} · {r['模型']}" for r in diag_rows],
        index=len(diag_rows) - 1,
    )
    sel = next(
        (s for s in snaps if s.get("confound") and f"{s.get('pool_label')} @ {s.get('trade_date')} · {s.get('model_run_id')}" == pick),
        None,
    )
    if sel and sel.get("deciles"):
        dec = pd.DataFrame(sel["deciles"])
        for col in ("mean_fwd", "median_fwd", "hit_tp", "hit_sl"):
            dec[col] = dec[col].astype(float) * 100.0
        dec = dec.rename(
            columns={
                "decile": "档", "n": "n", "mean_fwd": "收益均值", "median_fwd": "收益中位数",
                "hit_tp": "≥+8% 占比", "hit_sl": "≤−8% 占比", "mean_price": "均价(元)",
            }
        )
        st.dataframe(
            dec,
            use_container_width=True,
            hide_index=True,
            column_config={
                "收益均值": st.column_config.NumberColumn(format="%+.2f%%"),
                "收益中位数": st.column_config.NumberColumn(format="%+.2f%%"),
                "≥+8% 占比": st.column_config.NumberColumn(format="%.0f%%"),
                "≤−8% 占比": st.column_config.NumberColumn(format="%.0f%%"),
                "均价(元)": st.column_config.NumberColumn(format="%.1f"),
            },
        )
        st.caption("第 10 档均价明显低于第 1 档，就是「模型偏好低价股」在这张表里的样子。")
else:
    st.info("还没有走满 3 个交易日且行情覆盖足够的快照，诊断表暂时为空。")

section_header("四、建仓卡片对照基准", "止盈止损", level=2)
st.write(
    "收益按卡片 as_of 当日收盘到行情截止日收盘计，没有扣次日开盘价差、滑点和费用。"
    "「期末 ≥+8%」是到期末已达到止盈线的卡片占比，「路径触及」看持有期间任一天收盘有没有碰到 ±8%。"
)
adv_rows = []
for r in report.get("advice_summary") or []:
    adv_rows.append(
        {
            "as_of": r.get("as_of"),
            "池": POOL_LABEL.get(r.get("pool_id"), r.get("pool_id")),
            "卡片数": r.get("n_cards"),
            "有行情": r.get("n_priced"),
            "持有交易日": r.get("days_held"),
            "收益均值": _pct(r.get("mean_ret")),
            "收益中位数": _pct(r.get("median_ret")),
            "期末 ≥+8%": _pct(r.get("hit_tp")),
            "期末 ≤−8%": _pct(r.get("hit_sl")),
            "路径触及 +8%": _pct(r.get("touched_tp")),
            "路径触及 −8%": _pct(r.get("touched_sl")),
            "沪深300": _pct(r.get("hs300_ret")),
            "中证1000": _pct(r.get("csi1000_ret")),
            "同池等权": _pct(r.get("ew_pool_ret")),
            "超额(沪深300)": _pct(r.get("excess_vs_hs300")),
            "超额(等权)": _pct(r.get("excess_vs_ew")),
        }
    )
adv_df = pd.DataFrame(adv_rows)
pct_cols = [
    "收益均值", "收益中位数", "期末 ≥+8%", "期末 ≤−8%", "路径触及 +8%", "路径触及 −8%",
    "沪深300", "中证1000", "同池等权", "超额(沪深300)", "超额(等权)",
]
st.dataframe(
    adv_df,
    use_container_width=True,
    hide_index=True,
    column_config={k: st.column_config.NumberColumn(format="%+.2f%%") for k in pct_cols},
)
st.caption("「有行情」少于「卡片数」时，缺的那部分股票期末没有真实行情，不进均值；北交所 2026-09-24 之后行情只更新了十几只。")

detail = report.get("advice_detail") or []
if detail:
    with st.expander("每张建仓卡片的结果", expanded=False):
        det = pd.DataFrame(detail)
        for col in ("ret", "path_max", "path_min", "confidence"):
            if col in det.columns:
                det[col] = pd.to_numeric(det[col], errors="coerce") * 100.0
        det = det.rename(
            columns={
                "as_of": "as_of", "pool_id": "池", "symbol": "代码", "name": "名称", "confidence": "置信度",
                "price_start": "起点价", "ret": "收益", "path_max": "路径最高", "path_min": "路径最低",
                "hit_tp": "期末≥+8%", "hit_sl": "期末≤−8%", "touched_tp": "触及+8%", "touched_sl": "触及−8%",
                "is_st": "ST", "end": "截止",
            }
        )
        st.dataframe(
            det,
            use_container_width=True,
            hide_index=True,
            column_config={
                "收益": st.column_config.NumberColumn(format="%+.2f%%"),
                "路径最高": st.column_config.NumberColumn(format="%+.2f%%"),
                "路径最低": st.column_config.NumberColumn(format="%+.2f%%"),
                "置信度": st.column_config.NumberColumn(format="%.0f%%"),
            },
        )

section_header("五、这些数字有多少独立样本", None, level=2)
st.write(indep.get("note") or "—")
if indep.get("chosen_dates"):
    st.caption("入选的独立快照日：" + "、".join(indep["chosen_dates"]) + "。几组快照相隔不到 10 个交易日时只算一个。")

try:
    from common.db import get_connection

    _conn = get_connection(read_only=True, retries=3)
    try:
        render_trust_footer(_conn)
    finally:
        _conn.close()
except Exception:  # noqa: BLE001 - 仓库被写者占用时略过页脚，不红屏
    st.caption("数据仓库正在更新，页脚暂不显示。")
