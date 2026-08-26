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
    "RankIC": "衡量\"模型打分排序\"与\"股票实际后续收益排序\"有多match的指标，取值范围-1~1。"
              "大于0.03一般认为有一定选股能力，越接近1说明模型排的名次和实际涨跌顺序越吻合。",
    "IC": "全称Information Coefficient（信息系数），衡量因子/模型打分与未来收益的相关系数，"
          "越高说明这个信号越有效。",
    "因子": "从财务数据/价格数据里提炼出的、被历史证明与未来收益有一定关系的\"股票特征\"，"
            "比如\"过去12个月涨幅\"\"净资产收益率(ROE)\"，模型会综合很多个因子给股票打分。",
    "置信度": "模型对这条建议的相对把握程度（0~100%），不是\"胜率\"或\"成功概率\"——置信度越高，"
              "说明该股票在当天全市场排名越靠前，仅供参考排序，不是收益保证。",
    "VaR": "全称Value at Risk（风险价值），比如\"1日VaR(95%)=-3%\"的意思是：按最近一年的真实"
           "历史波动情况估计，未来1天里有95%的可能亏损不超过3%（也就是仍有5%的可能亏得更多）。",
    "CVaR": "在VaR基础上更进一步：如果真的出现了VaR预警的那种坏情况，平均会亏多少——比VaR更"
            "保守地衡量\"尾部风险\"。",
    "最大回撤": "从历史最高点到之后最低点，净值跌了多少——衡量\"最惨的时候能有多惨\"，回撤越小"
                "说明持仓过程中越少经历大幅亏损的煎熬。",
    "集中度": "某一只股票的持仓市值占你全部持仓总市值的比例，比例过高意味着这只股票一旦下跌，"
              "对你整体资产的拖累会很大（\"鸡蛋放一个篮子里\"）。",
    "冠军模型": "系统里当前正式使用、经过样本外验证效果最好的模型版本；每次训练出新模型，"
                "都要用同一套历史数据和评估方法\"赛马\"，显著更好才会替换掉当前冠军。",
    "PSI": "全称Population Stability Index，衡量\"现在的数据分布\"和\"训练模型时用的数据分布\""
           "差了多少——用来提前发现\"市场变了、模型可能不再适用\"的信号。",
    "止盈止损": "止损=亏到一定比例就卖出止血；止盈=赚到一定比例后见好就收。本系统的8%阈值"
                "跟训练模型标签用的阈值完全一致（不是另外拍的数字）。",
    "行为金融冲突信号": "从筹码分布、资金流、龙虎榜等数据里识别出的\"散户常见心理误区\"迹象"
                        "（比如过度追涨、羊群效应），提示你在决策时多一层留意，不是买卖指令。",
}


def apply_theme(page_title: str, page_icon: str = "📈", layout: str = "wide") -> None:
    """每个页面文件的第一句 Streamlit 调用必须是这个（取代直接调 st.set_page_config）。"""
    st.set_page_config(page_title=page_title, page_icon=page_icon, layout=layout)
    st.markdown(_CSS, unsafe_allow_html=True)


def action_meta(action: str) -> dict:
    return _ACTION_STYLE.get(action, {"color": "#616161", "bg": "#F5F5F5", "emoji": "•", "label": action})


def term_help(term: str, custom_text: str | None = None) -> None:
    """在关键指标旁放一个「？」小按钮，点开用大白话解释术语，不用跳出去查手册。"""
    text = custom_text or _GLOSSARY.get(term, "（暂无解释）")
    with st.popover(f"❓ {term}", use_container_width=False):
        st.markdown(f"**{term}**：{text}")


def glossary_dict() -> dict[str, str]:
    return dict(_GLOSSARY)


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


def render_advice_card(card: dict) -> None:
    """统一的建议卡片渲染：大字号动作标签 + 颜色分区 + 价格信息突出显示，
    取代原来纯文字expander的展示方式，降低非专业用户的理解门槛。"""
    meta = action_meta(card["action"])
    with st.container(border=True):
        head_col, conf_col = st.columns([4, 1])
        with head_col:
            st.markdown(
                f"<div class='yh-action-chip' style='background:{meta['bg']};color:{meta['color']};'>"
                f"{meta['emoji']} {card['symbol']} {card.get('name') or ''} —— {meta['label']}</div>",
                unsafe_allow_html=True,
            )
        with conf_col:
            st.metric("置信度", f"{card['confidence']:.0%}")

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
            st.markdown(f"<div class='yh-plain-summary'>💡 {card['plain_summary']}</div>", unsafe_allow_html=True)

        with st.expander("查看详细理由 / 风险提示 / 失效条件"):
            st.markdown("**理由**：")
            for r in card["reasons"]:
                st.write(f"- [{r['type']}] {r['detail']}")
            if card["risks"]:
                st.markdown("**⚠️ 风险提示**：")
                for r in card["risks"]:
                    st.write(f"- {r}")
            st.markdown("**该建议在什么情况下会失效**：" + "；".join(card["invalid_if"]))
            st.caption(card["disclaimer"])


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
