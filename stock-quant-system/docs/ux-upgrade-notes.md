# UI/UX 优化 + 一键启动打包 —— 工作记录（2026-08-26）

## 0. 背景与触发条件

Phase 4 人工可用性测试通过后，用户提出三点反馈：
1. UI页面字体普遍偏小，需要美化并放大字号；
2. 作为辅助买卖决策的工具，缺少更直观易懂的功能和说明；
3. 希望打包成 `.exe` 实现一键启动。

这不是新的Phase（对照 `docs/07-产品设计启示/03-MVP路线图.md`，Phase 0-4 已覆盖全部
Must/Should范围，之前评估过Phase4之后无新Phase待办），而是在既有Phase4应用层基础上的
**UX打磨与工程收尾**，因此不新开`phase5-acceptance-report.md`，改用本文档记录决策与验证过程，
遵循同样的"读需求-定方案-实现-测试打分-验收-留痕-清理"闭环。

## 1. 字体与主题（对应反馈1）

**方案**：`.streamlit/config.toml` 只能设颜色/字体家族，不支持直接设正文字号（Streamlit
框架本身限制）；因此新增 `common/ui_theme.py::apply_theme()` 用CSS注入放大字号
（正文18px，指标数字2rem粗体，表格16px，按钮/侧边栏导航同步放大），全部7个页面从这一个
入口接入，不在各页面重复写样式（复用原则）。

**验证**：用Playwright对7个页面逐一截图，人工检查字号明显大于默认Streamlit，且核心页面
（首页/持仓与建议/掘金扫描/风险仪表盘/行情图表/名词解释/历史建议复盘）均无渲染报错。

## 2. 决策工具直观易懂性优化（对应反馈2）

调研了当前建议卡片/页面的问题：只给百分比不给具体价格、术语无解释、看不到价格走势图、
无法验证过去建议是否靠谱、多个建议分散在不同页面不好一眼看全局。据此新增/改造：

| 改动 | 位置 | 解决的问题 |
|---|---|---|
| 具体价格换算 | `advice/advice_engine.py::price_levels()` | 百分比阈值换算成人民币止损/止盈价，不用自己心算 |
| 大白话摘要 | `advice/advice_engine.py::_plain_summary()` | 专业措辞之外配一句日常语言总结 |
| 今日决策速览 | `app.py` + `build_priority_digest()` | 建议分散在各页面，首页按紧急度汇总 |
| 行情图表页 | `pages/5_行情图表.py` | 看不到价格位置/止盈止损画在哪，纯文字不直观 |
| 名词解释页 + ❓气泡 | `pages/6_名词解释.py` + `common/ui_theme.py::term_help()` | RankIC/VaR/PSI等术语无解释 |
| 历史建议复盘页 | `pages/7_历史建议复盘.py` | 无法验证"过去的建议后来到底准不准"，缺乏信任基础 |

### 2.1 数据结构变更

`advice_log` 表新增 `name`/`plain_summary`/`price_levels_json` 三列（`common/db.py`
`_MIGRATIONS_SQL` 补充 `ALTER TABLE ... ADD COLUMN IF NOT EXISTS`，兼容已有数据库不炸表）。
`persist_advice_cards()` 的去重键由 `advice_id`（每次生成的随机uuid）改为
`(symbol, as_of)`——原设计下用户同一天多次点"刷新建议"会不断堆积重复历史记录，
2026-08-26 用真实点击测试（连续生成多次）复现到 `advice_log` 从预期的几十条膨胀到217条，
已修复并清理了本轮测试产生的重复数据（清理后35条，对应当天实际去重后的建议数）。

### 2.2 真实发现的Bug（如实记录，不是"跑起来不报错就算过"）

1. `pages/7_历史建议复盘.py` 首次实现用 `pd.merge_asof` 时报
   `MergeError: incompatible merge keys ... dtype('<M8[s]') vs dtype('<M8[us]')`——
   DuckDB DATE列经pandas转换后的datetime64精度与`pd.to_datetime()`默认精度不一致，
   `merge_asof`要求左右key精度完全相同。用Playwright真实跑通该页面时触发，不是凭空猜测。
   修复：两侧统一 `.astype("datetime64[us]")`。
2. 修复上一个问题后，同一处`merge_asof`又报`KeyError: 'symbol'`——因为参与merge的两个
   DataFrame都带`symbol`列，`merge_asof`默认加`_x`/`_y`后缀导致下游按原名取列失败。
   修复：merge前只保留`prices`的`trade_date`/`close`两列（`symbol`本来就已知，不需要
   再从`prices`里取一份）。
3. `advice_log`同日重复堆积（见2.1）——用真实多次点击"生成/刷新建议卡片"复现。

以上3个问题均通过 Playwright 真实点击+读取页面报错文本发现，修复后重新用 Playwright
逐一验证页面正常渲染、无Python traceback，符合"测试与打分"闸门要求。

## 3. 一键启动打包（对应反馈3）

**方案选择（工程决策，非能力不足的妥协）**：本项目依赖 torch/lightgbm/duckdb 等大体积、
含C扩展的库，若用PyInstaller把整个Streamlit应用连同这些依赖整体冻结进单文件exe，
社区已知会遇到两类问题：产物体积膨胀到数GB，以及Streamlit静态资源/torch动态库在frozen
模式下经常需要额外hook才能找到。因此采用更稳妥的方案：只把 `launcher.py` 这段轻量逻辑
（定位项目目录→调用项目自带`.venv`的python→跑`streamlit run app.py`→轮询端口→打开浏览器）
打包成exe，效果同样是"双击即用"，但不需要把几GB的深度学习库塞进一个exe。

- 打包脚本：`scripts/build_exe.py`（封装 `pyinstaller --onefile --console`，产物复制到
  项目根目录 `研衡启动器.exe` 后自动清理中间文件`_dist_tmp`/`_build_tmp`）。
- **真实验证**：先手动kill占用8501端口的旧进程，双击/运行打包好的 `研衡启动器.exe`，
  确认它成功拉起了新的streamlit子进程（`netstat`确认监听8501）、`curl`确认HTTP 200，
  再用Playwright打开`http://localhost:8501`确认首页（含"今日决策速览"）正常渲染、
  无Python报错——不是"打包完看目录里有exe就算完成"。

**已知局限（如实记录）**：exe本身不携带Python依赖，换一台新电脑分发时仍需先完整走一遍
`python -m venv .venv` + `pip install -r requirements.txt` 的环境搭建；这是设计取舍
（详见开发者说明书9a节），不是遗漏。

## 4. 清理

本轮未引入一次性脚本（`scripts/build_exe.py`是可复用的常驻工具，不属于清理对象）；
`_dist_tmp`/`_build_tmp` 打包中间产物已在 `build_exe.py` 内自动清理；测试期间产生的
`advice_log` 重复记录已用一次性SQL去重清理（未留脚本文件）；发现并删除了仓库内重复的
`stock-quant-system/.gitignore`（与仓库根目录 `.gitignore` 功能重复，统一以根目录为准）。

## 5. 文档同步

- `README.md` 新增"第12节：可用性测试通过后的UI/UX优化"，更新目录结构说明。
- `scripts/build_manuals.py` 更新用户手册（新增页面用法、一键启动器说明、具体价格换算
  说明）与开发者手册（新增9a节打包工程决策），已重新生成
  `docs/manuals/用户使用说明书.docx` / `开发者使用说明书.docx`。
- `requirements.txt` 补充 `python-docx`（手册生成用）与 `pyinstaller` 按需安装说明。

## 6. 设计理论复核（2026-08-26 第二轮）

用户提供了一份独立的UI/UX设计理论库（`E:\武_创赛\03_素材库\设计理论库_完整版`，共13个
分册，聚焦认知负荷/Gestalt/信息图示/无障碍对比度/栅格/色彩Token/字体层级/叙事结构/图标
一致性/品牌协作等理论，原始定位是路演PPT设计指导，但其中大部分是通用视觉设计原理，可以
迁移到本项目的Streamlit界面上）。逐册通读后，只挑了**能落地成代码、有实测证据支撑**的几条
去改，不是照单全收（比如"投影环境校准"这类纯PPT场景的建议就不适用，跳过）。

| 理论分册 | 发现的问题 | 具体改动 |
|---|---|---|
| `04_无障碍对比度与投影.md`（WCAG 2.2 AA） | 之前的"减仓"文字色`#E65100`在其底色`#FFF3E0`上实测对比度只有**3.46:1**，不达AA正文≥4.5:1标准；"止盈"色`#2E7D32`实测4.56:1贴线无余量 | 用相对亮度公式实测全部7种操作颜色（脚本见下），把"减仓"改深到`#BF360C`（5.11:1），"止盈"改深到`#1B5E20`（7.00:1），其余5种色实测本来就达标，不动 |
| `08_字体层级与中文排版.md`（Modular Scale） | 原有h1~h4字号（2.1/1.7/1.4/1.2rem）是当时随手取的四个数，级间比例不统一（1.17~1.24之间跳动） | 改成以正文18px为基准、公比1.25（Major Third，理论库原文建议值）逐级换算：18→22.5→28→35→44px，四级标题第一次有统一、可解释的比例关系，且换算结果恰好落在该分册"卡片标题20-24/页标题28-36/封面44-52"建议区间内 |
| `03_信息图示与数据墨水.md`（Tufte/Few：饼图≤4片，否则改水平柱） | `pages/3_风险仪表盘.py`的持仓集中度图用饼图，实际持仓常常不止4只，角度差本来就不好精确比较 | 改成按占比排序的水平柱状图，柱上直接标百分比数值（Tufte"直接标签优于图例"），叠加一条15%风控上限的虚线+文字标注，一眼能看出谁超线 |
| `02_Gestalt与视觉组织.md`（相似/对齐：同类元素应长得一样） | 多个页面各自手写`st.columns([5,1])`拼"标题+❓帮助按钮"，比例、写法不统一 | 新增`common/ui_theme.py::section_header()`统一封装，`app.py`/`pages/3_风险仪表盘.py`/`pages/7_历史建议复盘.py`里原本重复的写法改为调用它 |
| `06_栅格间距安全区与对齐.md`（8pt基线/卡片内边距16-24pt） | 卡片、卡片间距此前依赖Streamlit默认值，未显式统一 | CSS里显式给`st.container(border=True)`卡片内边距16px、卡片间gap 16px，取8的倍数 |

**对比度实测方法**（不是凭肉眼判断，遵循vibe coding八荣八耻第7条"不懂装懂为耻"）：用
WCAG标准的相对亮度公式（`L = 0.2126R + 0.7152G + 0.0722B`，RGB先做sRGB→linear转换）
写了一个约20行的临时Python脚本，对`_ACTION_STYLE`里每一对文字色/底色算真实对比度，
用完即删（未留在仓库里）：

| 颜色对 | 用途 | 实测对比度 | AA达标(≥4.5:1)? |
|---|---|---|---|
| `#C62828` on `#FDEDEC` | 止损 | 4.95:1 | 是 |
| `#E65100` on `#FFF3E0`（改前） | 减仓 | 3.46:1 | **否** |
| `#BF360C` on `#FFF3E0`（改后） | 减仓 | 5.11:1 | 是 |
| `#2E7D32` on `#E8F5E9`（改前） | 止盈 | 4.56:1 | 是（贴线） |
| `#1B5E20` on `#E8F5E9`（改后） | 止盈 | 7.00:1 | 是（留余量） |
| `#6A1B9A` on `#F3E5F5` | 再平衡 | 7.75:1 | 是 |
| `#1565C0` on `#E3F2FD` | 继续持有 | 5.03:1 | 是 |
| `#00695C` on `#E0F2F1` | 可考虑建仓 | 5.71:1 | 是 |
| `#616161` on `#F5F5F5` | 观望 | 5.68:1 | 是 |

**验证**：重启Streamlit后用Playwright重新逐页截图+读取Plotly图表底层trace数据核实——
直接检查了持仓集中度图的`type`字段确实是`"bar"`（不是`"pie"`）、`orientation: "h"`、
柱子按百分比升序排列、`text`字段有百分比标签、存在`x=0.15`的红色虚线shape与"15%风控
上限"标注；`section_header()`改造后的三处页面标题+❓按钮渲染正常，点击❓能正常弹出术语
解释popover。

**事后更正（2026-08-26 晚）**：上述 Plotly 结构检查没有发现「y 轴把 6 位股票代码当连续数字」
——用户截图纵坐标出现「20万/40万/60万」，柱子几乎看不见。根因是 pandas/Plotly 把
`600519` 当成数值，轴范围被拉到六十万。已强制 `yaxis.type="category"`，并在
`compute_concentration` 把代码规范成 6 位字符串；超限柱改红色、轴范围按最大占比留白、
图下增加对照表。回归见 `scripts/test_concentration.py`。

## 7. 全流程边界测试（2026-08-26）

用户要求"检测全流程、各个功能与相应边界"。本轮UI改动只涉及`common/ui_theme.py`、
`app.py`、3个页面文件，未触碰`ingestion/`/`research/`/`mlops/`任何一行代码，所以没有
必要重新跑一遍全量数据回填或模型训练（那些模块的验收结果记录在Phase 0-3各自的验收报告
里，本轮无改动=无需重新验证），边界测试聚焦"本轮改动可能影响到的应用层"，具体覆盖：

| 场景 | 页面 | 结果 |
|---|---|---|
| 持仓表单：股票代码留空 / 股数=0 | `pages/1_持仓与建议.py` | 友好提示"股票代码、股数、成本价均为必填且需大于0"，未抛异常 |
| 查询不存在的股票代码（999999） | `pages/5_行情图表.py` | 提示"没有查到这只股票的行情数据"，未抛异常 |
| AI解释页输入无效代码（abcdef/000000） | `pages/4_AI解释.py` | 正常渲染，未抛异常（结构化数据区块如实显示空值） |
| 名词解释搜索无匹配关键词 | `pages/6_名词解释.py` | 结果列表为空，页面其余部分正常 |
| 掘金扫描滑块取最小值10/最大值200 | `pages/2_掘金扫描.py` | 两次扫描均正常完成并出结果 |
| 历史复盘筛选器清空所有选项 | `pages/7_历史建议复盘.py` | 发现原实现清空后是空白无提示——已修复，补充"没有符合当前筛选条件的记录"提示 |
| 风险仪表盘常规重新加载 | `pages/3_风险仪表盘.py` | 无回归，集中度图/VaR/相关性矩阵均正常 |

同时核对了`advice_log`表结构迁移（新增`name`/`plain_summary`/`price_levels_json`列）
影响范围：全仓库grep确认只有`pages/7_历史建议复盘.py`、`scripts/build_manuals.py`、
`advice/advice_engine.py`、`common/db.py`四处引用该表，`scripts/daily_pipeline.py`等
数据管道脚本不依赖`advice_log`，本轮改动不存在跨模块破坏性影响的边界风险。

**追查并修复了一个真实bug**：`历史建议复盘`页发现一条历史记录（920471）的股票名称显示为
字面的"nan"。查库确认`universe`表里该代码name字段并不缺失（"美邦科技"），根因在
`pages/7_历史建议复盘.py`第114行的`row.get('name') or ''`——pandas的`NaN`在Python里是
"真值"（不是`None`/`False`/`0`/`''`），对`name`列为`NaN`的记录（`advice_log`新增`name`
列之前生成的历史记录，迁移不会回填旧记录），`NaN or ''`求值结果还是`NaN`，被拼进f-string
后就显示成字面"nan"。用`pd.notna()`显式判断修复，`plain_summary`同一模式的隐患一并修复。

## 8. 按名称查代码（2026-08-26）

复用 `universe.name`，模块 `common/symbol_lookup.py`。接到首页、持仓录入、行情图、AI 解释。
内存 DuckDB 单测覆盖：紫金矿业→601899、中信多匹配、6 位代码精确命中。未在占用中的主库上做
端到端点击（当时 daily_pipeline 独占仓库）。

## 9. LLM「停在 2024」不是断网（2026-08-26）

用户反馈通义像断网、时间认知停在 2024。**原因**：qwen-plus / DeepSeek 都是聊天模型，
没有实时日历，训练截止日期之后的「今天」会用参数记忆瞎补；东方财富标题常写成 `08-21`
不带年份，模型就填成 2024。**不是** API 离线。

**为什么不换 DeepSeek、不开 enable_search**：换模型解决不了对时；官方联网搜索在
OpenAI 兼容接口上不返回来源（help.aliyun.com/zh/model-studio/web-search），会破坏
「只翻译已提供、可追溯输入」的翻译层边界。新闻已经由 `llm/news_fetch.py` 即时抓取。

**修法**：`explain_assistant._system_prompt(as_of)` 写入本机日期；`build_context_text`
带新闻 `publish_time` 和 `prediction_log.trade_date`。回归：`scripts/test_llm_explain.py`。

## 10. Phase 5 散户决策链 UI（2026-09-17）

- 建议卡片：`render_advice_card` 展示股数/金额/执行日；`render_trust_footer` 统一数据截止与滑点说明（首页、持仓、复盘、待办）。
- 持仓页：生成建议后「批量模拟成交」；模拟盘 expander 挂 `paper_weekly_report` 四指标。
- 复盘页：S7 成交价相对建议日收盘偏差表。
- 验收：`docs/phase5-acceptance-report.md` §6；测例 `test_paper_batch_simulate.py`。
