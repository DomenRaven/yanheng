"""
全局UI主题与放大字体：所有 `app.py` / `pages/*.py` 统一从这里调 `apply_theme()`，
不在各页面里重复写CSS（对齐vibe coding八荣八耻第4条"复用存量"）。

**为什么用CSS注入而不是只改`.streamlit/config.toml`**：Streamlit的`[theme]`配置项
（`.streamlit/config.toml`）只能改颜色/字体家族，不支持直接设置正文字号——这是Streamlit
框架本身的限制，不是本项目遗漏。放大字号只能通过`st.markdown(<style>...)`注入CSS覆盖
默认样式实现，两者需要配合使用（config.toml管颜色，本模块管字号与卡片样式）。

**本轮设计依据（2026-08-26 对照 `E:\\武_创赛\\03_素材库\\设计理论库_完整版` 理论库
逐条核对后调整，不是随手改颜色/字号，可追溯到具体分册）**：
- 字号阶梯：`08_字体层级与中文排版.md` 建议用Modular Scale（Major Third 1.25倍）建立
  层级，而不是每级随手取一个数字——本模块 `h1~h4` 就是以正文18px为基准、按1.25倍逐级
  递增算出来的（18→22.5→28→35→44px），换算后恰好落在该分册"卡片标题20-24/页标题
  28-36/封面44-52"的建议区间里，不是巧合，是刻意对齐。
- 色彩对比度：`04_无障碍对比度与投影.md` 引用WCAG 2.2 AA标准（正文≥4.5:1，18px粗体
  这类"准大字"也按4.5:1从严要求，不套用3:1大字标准留安全余量）。下面`_ACTION_STYLE`
  里每一对文字色/底色的对比度都用相对亮度公式实测算过（`docs/ux-upgrade-notes.md`
  第2轮记录了具体数值），不是凭肉眼感觉选的颜色；`reduce`/`take_profit`两个色因为
  实测未达标已经调深。
- 间距与卡片：`06_栅格间距安全区与对齐.md` 建议卡片内边距16-24px、8pt为间距基线，
  下面CSS的padding/gap都取这个区间内的8的倍数。
- Gestalt对齐：`02_Gestalt与视觉组织.md` 的"邻近/相似/对齐"三条——本模块新增的
  `section_header()` 把"标题+❓帮助按钮"这个重复出现在多个页面的组合固定成统一比例，
  避免各页面各写一套column比例导致"同类元素长得不一样"。

**目标人群**：非专业个人投资者，年龄跨度较大，默认字号需比Streamlit原生偏大一档，
关键数字（价格/涨跌幅/置信度）需要比正文更大更醒目。
"""
from __future__ import annotations

import streamlit as st

# 色彩对比度均实测：数值见 docs/ux-upgrade-notes.md「设计理论复核」一节，
# 对照 WCAG 2.2 AA 正文标准(≥4.5:1)逐一验证，不是凭观感取色。
_ACTION_STYLE = {
    "stop_loss": {"color": "#C62828", "bg": "#FDEDEC", "emoji": "🛑", "label": "止损"},       # 4.95:1
    "reduce": {"color": "#BF360C", "bg": "#FFF3E0", "emoji": "⚠️", "label": "减仓"},          # 5.11:1（原#E65100仅3.46:1不达标，已调深）
    "take_profit": {"color": "#1B5E20", "bg": "#E8F5E9", "emoji": "💰", "label": "止盈"},     # 7.00:1（原#2E7D32仅4.56:1贴线，留安全余量调深）
    "rebalance": {"color": "#6A1B9A", "bg": "#F3E5F5", "emoji": "⚖️", "label": "再平衡"},     # 7.75:1
    "hold": {"color": "#1565C0", "bg": "#E3F2FD", "emoji": "✅", "label": "继续持有"},         # 5.03:1
    "open": {"color": "#00695C", "bg": "#E0F2F1", "emoji": "🟢", "label": "可考虑建仓"},       # 5.71:1
    "watch": {"color": "#616161", "bg": "#F5F5F5", "emoji": "👀", "label": "观望"},           # 5.68:1
}

_GLOSSARY: dict[str, str] = {
    "排名相关系数": "衡量「模型排的名次」与「之后实际涨跌顺序」有多接近，取值约在 -1 到 1。"
                   "大于 0.03 通常说明有一定选股区分度；越接近 1，名次与后来涨跌顺序越接近。",
    "信息系数": "衡量某个因子或模型分数与未来收益的相关程度。数值越高，说明该信号越有参考价值。",
    "因子": "从财务或价格数据里提炼出的股票特征，例如「过去 12 个月涨幅」「净资产收益率」。"
            "模型会综合多个因子给股票打分排序。",
    "置信度": "按当天全市场排名分位换算得到的相对把握（0%~100%）。数值越高，表示该股票当日排名越靠前。"
              "请按排序参考理解，勿当作胜率或盈利概率。",
    "风险价值": "按最近约一年真实日收益估算的风险水平。例如「1 日风险价值（95%）=-3%」表示："
                "在历史波动情形下，约有 95% 的可能一天亏幅不超过 3%；仍有约 5% 的可能亏得更多。",
    "条件风险价值": "在已经出现「风险价值」所预警的不利情形时，平均还会再亏多少。用来更保守地看尾部亏损。",
    "最大回撤": "从历史最高点到之后最低点，净值一共跌了多少。数值越小，说明持仓过程中大幅回落越少。",
    "集中度": "某一只股票的市值 ÷ 您全部持仓市值之和。单票超过 15% 视为过重。"
              "现金未计入分母，请自行预留。",
    "冠军模型": "当前正式使用的模型版本。周末重训出的「挑战者」须用同一套 Walk-Forward 样本外 "
                "RankIC 与现任比较，达标才写入 champion_hs.json / champion_bj.json。"
                "其它 run 文件夹只是历史留痕；扫描不会自动用「最新文件夹」。"
                "沪深与北交所各一份冠军，互不串池。",
    "分池扫描": "沪深股票与北交所股票分开排序：沪深用沪深冠军，北交所用北交所冠军。"
                "页脚与首页会分别显示两池 run 编号与训练面板区间；避免微盘风格淹没主板名单。",
    "坚持度": "多次掘金扫描中反复出现在前 50 名的次数。用来观察名单是否稳定，"
              "不是荐股，也不是胜率。",
    "分布稳定性": "衡量「当前数据分布」与「训练模型时的数据分布」差了多少，用来提前发现市场结构变化。",
    "止盈止损": "止损：亏到约定比例后卖出止血；止盈：赚到约定比例后减仓锁定。本系统默认 8%，"
                "与训练模型标签使用的阈值一致。",
    "行为冲突提示": "根据筹码、资金流、龙虎榜等数据识别出的常见心理误区迹象（如过度追涨、羊群效应）。"
                    "仅作提醒，请自行结合卡片数字判断。",
    # 兼容旧「❓」按钮仍可能传入的英文键
    "RankIC": "见「排名相关系数」。",
    "IC": "见「信息系数」。",
    "VaR": "见「风险价值」。",
    "CVaR": "见「条件风险价值」。",
    "PSI": "见「分布稳定性」。",
    "行为金融冲突信号": "见「行为冲突提示」。",
}


def apply_theme(page_title: str, page_icon: str = "📈", layout: str = "wide") -> None:
    """每个页面文件的第一句 Streamlit 调用必须是这个（取代直接调 st.set_page_config）。"""
    st.set_page_config(page_title=page_title, page_icon=page_icon, layout=layout)
    st.markdown(_CSS, unsafe_allow_html=True)
    _render_sidebar_status()


@st.cache_data(ttl=90, show_spinner=False)
def _cached_warehouse_readiness() -> dict:
    """侧边栏用：短 TTL 缓存，避免每页重复全量 assess。"""
    from common.db import get_ui_connection, init_schema
    from common.warehouse_readiness import assess_warehouse_readiness

    conn = get_ui_connection()
    try:
        init_schema(conn)
        rep = assess_warehouse_readiness(conn)
        return {
            "ready": rep.ready,
            "blockers": list(rep.blockers),
            "warnings": list(rep.warnings),
            "metrics": dict(rep.metrics),
            "update_in_progress": rep.update_in_progress,
            "recommended_command": rep.recommended_command,
        }
    finally:
        conn.close()


def _render_sidebar_status() -> None:
    with st.sidebar:
        st.markdown("### 研衡 · 数据状态")
        try:
            snap = _cached_warehouse_readiness()
        except Exception as exc:
            st.error(f"无法读取仓库：{exc}")
            return
        if snap.get("update_in_progress"):
            st.info("数据更新进行中（部分功能只读）")
        if snap.get("ready"):
            eff = snap.get("metrics", {}).get("effective_quote_date", "—")
            st.success(f"数据可用 · 行情截面 {eff}")
        else:
            st.error("数据未就绪")
            for b in (snap.get("blockers") or [])[:3]:
                st.caption(f"· {b}")
        lag = snap.get("metrics", {}).get("quote_lag_trading_days")
        if lag is not None:
            st.caption(f"相对最近交易日滞后：{lag} 个交易日")
        if st.button("刷新数据状态", key="sidebar_refresh_wh"):
            _cached_warehouse_readiness.clear()
            st.rerun()
        st.divider()
        st.caption(
            "常用路径：持仓与建议 → 明日待办 → 模拟盘或复盘。"
            " 收盘后数据更新说明见仓库文档「投产日课」。"
        )


def connect_warehouse():
    """打开仓库；文件锁时给出中文说明而不是红屏 traceback，然后 st.stop()。"""
    from common.db import WarehouseBusyError, get_ui_connection, init_schema, is_read_only

    try:
        conn = get_ui_connection()
    except WarehouseBusyError:
        st.error(
            "数据仓库正在后台更新，暂时打不开。"
            "行情抓取会在每只股票的网络请求间隙释放文件锁，请过几秒刷新本页。"
        )
        st.stop()
    if is_read_only(conn):
        st.warning(
            "数据正在后台更新，当前为只读浏览。"
            "录入持仓、生成建议等写入操作请等更新结束后再试。"
        )
    else:
        init_schema(conn)
    return conn


def action_meta(action: str) -> dict:
    return _ACTION_STYLE.get(action, {"color": "#616161", "bg": "#F5F5F5", "emoji": "•", "label": action})


def term_help(term: str, custom_text: str | None = None) -> None:
    """在关键指标旁放一个「？」小按钮，点开用大白话解释术语，不用跳出去查手册。"""
    text = custom_text or _GLOSSARY.get(term, "（暂无解释）")
    with st.popover(f"❓ {term}", use_container_width=False):
        st.markdown(f"**{term}**：{text}")


def glossary_dict() -> dict[str, str]:
    """供名词解释页展示：只返回中文主词条，避免英文别名占满列表。"""
    skip = {"RankIC", "IC", "VaR", "CVaR", "PSI", "行为金融冲突信号"}
    return {k: v for k, v in _GLOSSARY.items() if k not in skip}


def section_header(title: str, help_term: str | None = None, level: int = 3) -> None:
    """标题 + 可选的「❓」术语按钮，统一比例（Gestalt"相似"原则——同类组合在所有页面
    保持同一种排布，读者一眼就认得出这是"一个小节的标题行"，不用每页重新判断布局）。
    取代此前 `pages/*.py` 里各自手写 `st.columns([5,1])` 的重复写法。"""
    if help_term:
        head_col, help_col = st.columns([6, 1])
        with head_col:
            _heading(title, level)
        with help_col:
            st.write("")
            term_help(help_term)
    else:
        _heading(title, level)


def _heading(title: str, level: int) -> None:
    if level == 1:
        st.header(title)
    elif level == 2:
        st.subheader(title)
    else:
        st.markdown(f"#### {title}")


def render_advice_card(card: dict, *, details: str = "expander") -> None:
    """统一的建议卡片渲染。

    details:
      - ``expander``：顶层卡片可用（勿再包进外层 expander，否则会触发 React removeChild）
      - ``toggle``：嵌套场景用 checkbox，避免 expander 套 expander
      - ``none``：只显示摘要，不展开明细
    """
    meta = action_meta(card["action"])
    aid = str(card.get("advice_id") or card.get("symbol") or id(card))
    with st.container(border=True):
        head_col, conf_col = st.columns([4, 1])
        with head_col:
            st.markdown(
                f"**{meta['emoji']} {card['symbol']} {card.get('name') or ''} —— {meta['label']}**"
            )
        with conf_col:
            st.metric("置信度", f"{card['confidence']:.0%}")
            st.caption("按模型排名分位换算，供排序参考；请勿当作胜率或盈利概率。")

        price_levels = card.get("price_levels")
        if price_levels:
            p1, p2, p3, p4 = st.columns(4)
            p1.metric("现价", f"¥{price_levels['last_close']:.2f}" if price_levels.get("last_close") is not None else "—")
            if price_levels.get("cost_price") is not None:
                p2.metric("成本价", f"¥{price_levels['cost_price']:.2f}")
            if price_levels.get("stop_loss_price") is not None:
                p3.metric("参考止损价", f"¥{price_levels['stop_loss_price']:.2f}")
            if price_levels.get("take_profit_price") is not None:
                p4.metric("参考止盈价", f"¥{price_levels['take_profit_price']:.2f}")

        if card.get("plain_summary"):
            # 避免复杂 HTML 与大量卡片叠加重绘；用原生 markdown
            st.info(f"💡 {card['plain_summary']}")
        if card.get("reason_one_liner"):
            st.caption(card["reason_one_liner"])

        if card.get("size_shares") and float(card["size_shares"]) >= 100:
            s1, s2, s3, s4 = st.columns(4)
            s1.metric("建议股数", f"{int(card['size_shares'])} 股")
            if card.get("est_amount_cny") is not None:
                s2.metric("约需资金", f"¥{float(card['est_amount_cny']):,.0f}")
            pct = card.get("size_pct_nav")
            if isinstance(pct, list):
                pct = pct[1] if len(pct) > 1 else pct[0]
            if pct is not None and not isinstance(pct, list):
                s3.metric("占净值约", f"{float(pct):.1%}")
            if card.get("max_loss_cny") is not None and float(card["max_loss_cny"]) > 0:
                s4.metric("参考最大亏损", f"¥{float(card['max_loss_cny']):,.0f}")
        if card.get("exec_date"):
            st.caption(
                f"建议执行日（下一交易日）：{card['exec_date']}；"
                f"持有参考周期 {card.get('horizon_days') or '—'} 个交易日"
            )

        def _render_details() -> None:
            st.markdown("**理由**：")
            for r in card.get("reasons") or []:
                st.write(f"- [{r.get('type')}] {r.get('detail')}")
            risks = card.get("risks") or []
            if risks:
                st.markdown("**⚠️ 风险提示**：")
                for r in risks:
                    st.write(f"- {r}")
            inv = card.get("invalid_if") or []
            if inv:
                st.markdown("**该建议在什么情况下会失效**：" + "；".join(inv))
            st.caption(card.get("disclaimer") or "仅供研究辅助，不构成投资建议")

        if details == "expander":
            with st.expander("查看详细理由 / 风险提示 / 失效条件", key=f"card_exp_{aid}"):
                _render_details()
        elif details == "toggle":
            if st.checkbox("查看详细理由 / 风险 / 失效条件", key=f"card_tog_{aid}"):
                _render_details()


def warehouse_data_end_date(conn) -> str | None:
    """前复权日线最新交易日（供页脚与投产告警共用）。"""
    try:
        row = conn.execute(
            "SELECT MAX(trade_date) FROM daily_quotes WHERE adjust = 'qfq'"
        ).fetchone()
        if row and row[0] is not None:
            return str(row[0])[:10]
    except Exception:
        pass
    return None


def render_production_banners(conn) -> None:
    """投产环境：灌库只读 + 空库/滞后等投产阻塞说明。"""
    from common.db import is_read_only
    from common.warehouse_readiness import assess_warehouse_readiness

    if is_read_only(conn):
        st.info(
            "后台数据更新进行中：本页只读，暂不能录入持仓、生成建议或模拟成交。"
            "更新结束后请刷新页面。"
        )

    report = assess_warehouse_readiness(conn)
    if report.ready:
        eff = report.metrics.get("effective_quote_date")
        exp = report.metrics.get("expected_latest_trade_date")
        if eff:
            st.caption(
                f"当前可用行情截面日：**{eff}**"
                + (f"（最近交易日 {exp}）" if exp else "")
                + " · 按日频更新，不含盘中逐笔"
            )
        for w in report.warnings:
            st.warning(w)
        return

    st.error("**数据未就绪**：本地行情库尚未达到生成建议的门槛。")
    for b in report.blockers:
        st.markdown(f"- {b}")
    for w in report.warnings:
        st.warning(w)
    st.code(report.recommended_command, language="powershell")
    st.caption("可在项目目录执行上述命令完成收盘后更新；详细步骤见仓库文档「投产日课」。")


def require_warehouse_for_decisions(conn) -> None:
    """扫描/建议/待办：未就绪则中断页面。"""
    from common.warehouse_readiness import assess_warehouse_readiness

    if assess_warehouse_readiness(conn).ready:
        return
    render_production_banners(conn)
    st.stop()


def render_trust_footer(conn) -> None:
    """M17：统一展示数据截止、分池冠军摘要、模拟成交假设（只读查询）。"""
    from common.config import get_config
    from advice.champion_registry import describe_champion

    data_end = warehouse_data_end_date(conn) or "—"
    try:
        from common.warehouse_readiness import effective_market_quote_date

        eff = effective_market_quote_date(conn)
        if eff:
            data_end = str(eff)
    except Exception:
        pass

    hs = describe_champion(pool_id="hs")
    bj = describe_champion(pool_id="bj")

    pt = get_config().get("paper_trading") or {}
    slip = pt.get("slippage_bp", "—")
    allow_bj = bool(pt.get("allow_bj_open_in_todos", False))
    st.divider()
    st.caption(
        f"数据截止（前复权日线）：{data_end} · "
        f"{hs['short']} · {bj['short']} · "
        f"模拟：默认次日开盘价 ±{slip}bp（非盘口）；一键练习仅明日待办≤3；"
        f"待办北交所开仓：{'开' if allow_bj else '关'} · "
        "输出仅供个人研究辅助；模拟净值请勿直接当作实盘收益预期。"
    )


def close_warehouse(conn) -> None:
    """页末统一合规说明并关闭连接。"""
    render_trust_footer(conn)
    conn.close()


_CSS = """
<style>
/* ==================================================================
   字号阶梯：Modular Scale，公比1.25（Major Third，见理论库08分册），
   以正文18px为基准逐级 ×1.25：18 → 22.5 → 28 → 35 → 44px。
   换算后落点恰好对齐该分册"卡片标题20-24 / 页标题28-36 / 封面44-52"的
   建议区间，四级标题之间的比例是刻意算出来的，不是四个随意数字。
   ================================================================== */
html, body, [class*="st-"], .stMarkdown, .stText, p, li, span, label {
    font-size: 18px !important;   /* 正文下限值，理论库08分册："正文18-22，最低18" */
    line-height: 1.65 !important;
}
h1 { font-size: 2.444rem !important; }  /* 44px：封面/品牌级标题 */
h2 { font-size: 1.944rem !important; }  /* 35px：页标题 */
h3 { font-size: 1.556rem !important; }  /* 28px：页标题下限/小节标题 */
h4 { font-size: 1.25rem  !important; }  /* 22.5px：卡片标题 */

/* 指标（st.metric）数字加大加粗，落在理论库"KPI 32-48px"区间内，是用户最常扫一眼看的东西 */
div[data-testid="stMetricValue"] { font-size: 2rem !important; font-weight: 700 !important; }
div[data-testid="stMetricLabel"] { font-size: 1rem !important; color: #55606B !important; }
div[data-testid="stMetricDelta"] { font-size: 1rem !important; }

/* 表格/dataframe 字号同步放大 */
[data-testid="stDataFrame"] * { font-size: 16px !important; }

/* 按钮更大更好点 */
.stButton > button, .stFormSubmitButton > button {
    font-size: 17px !important;
    padding: 0.55rem 1.2rem !important;
    border-radius: 8px !important;
}

/* 侧边栏导航文字放大 */
[data-testid="stSidebarNav"] span { font-size: 17px !important; }

/* Tabs 标签放大 */
button[data-baseweb="tab"] { font-size: 17px !important; }

/* ---- 间距：8pt基线网格（理论库06分册"8pt Grid / Bento padding 16-24pt"） ----
   卡片(st.container(border=True))内边距、卡片之间的间距统一取8的倍数，
   不同页面之间不再各自随手留白，Gestalt"邻近"法则要求的疏密对比才稳定。 */
[data-testid="stVerticalBlockBorderWrapper"] { padding: 16px !important; }
[data-testid="stVerticalBlock"] { gap: 16px !important; }

/* 建议卡片里的动作标签 chip：字号刻意选在"18.66px粗体"以上，配合评审过的
   对比度数值，同时满足WCAG大字文本≥3:1与本模块从严采用的≥4.5:1两条线。 */
.yh-action-chip {
    display: inline-block;
    font-size: 1.3rem !important;
    font-weight: 700;
    padding: 0.35rem 0.9rem;
    border-radius: 10px;
    margin-bottom: 0.4rem;
}

/* 大白话摘要条：#FFFDE7底 + 深色正文，实测对比度14.8:1，远超AA标准 */
.yh-plain-summary {
    background: #FFFDE7;
    border-left: 4px solid #FBC02D;
    padding: 0.6rem 0.9rem;
    border-radius: 6px;
    margin: 0.5rem 0;
    font-size: 17px !important;
}

/* 免责声明/说明文字caption稍微放大，不然几乎看不清 */
[data-testid="stCaptionContainer"], .stCaption { font-size: 15px !important; }
</style>
"""
