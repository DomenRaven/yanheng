# 研衡 YanHeng —— 个人量化研究系统

产品名 **研衡**：研（可复现的投研）+ 衡（风控优先、仓位可度量）。代码目录仍为 `stock-quant-system/`。

对应总体方案：`c:\Users\MAC\.cursor\plans\个人炒股辅助本地应用_76077b1a.plan.md`
理论依据：`../docs/`（本仓库上一级的理论库，见 `../README.md`）

当前进度：**Phase 0 / 0.5 / 0.6 / 0.7 / 1 / 2 / 3 / 4 全部验收完成**（因子研究+LightGBM排序模型+
全市场扫描首轮落地，样本外多头组合扣费后相对基准超额收益统计显著 p=0.0044；模型生命周期
冠军-挑战者机制+漂移监控+行为金融代理信号层已接入掘金扫描；深度学习GNN挑战者已同协议评估，
结论是维持LightGBM为生产冠军；Phase 4持仓驾驶舱+场景化建议引擎+组合优化再平衡+风险仪表盘+
LLM解释层已上线Streamlit本地应用，对齐`docs/00-总览/02-产品定位与边界.md`全部Must/Should
条款）。项目按MVP路线图（`docs/07-产品设计启示/03-MVP路线图.md`）四个阶段的Must/Should范围
已全部覆盖，Won't清单（自动下单/高频/收益承诺）严格未触碰。

- Phase 0-0.7 验收结果与真实缺口清单：`docs/phase0-acceptance-report.md`
- Phase 1 因子/模型/回测结果与方法论边界：`docs/phase1-acceptance-report.md`
- Phase 2 模型生命周期管理+行为金融信号层结果：`docs/phase2-acceptance-report.md`
- Phase 3 深度学习挑战者评估结果（结论：维持LightGBM基线）：`docs/phase3-acceptance-report.md`
- Phase 4 应用层（持仓/建议/风险/组合优化/LLM解释）验收结果：`docs/phase4-acceptance-report.md`
- 可用性测试通过后的UI优化（放大字体/建议卡片改版/今日决策速览/行情图表/名词解释/历史建议复盘/
  一键启动exe打包）：`docs/ux-upgrade-notes.md`
- 用户手册 / 开发者手册（Word）：`docs/manuals/用户使用说明书.docx`、`docs/manuals/开发者使用说明书.docx`
- 本项目的开发工作流与道德约束：`.cursor/rules/quant-dev-loop.mdc`、
  `.cursor/rules/vibe-coding-ethics.mdc`（工作区根目录）

## 1. 环境搭建记录

- Python 3.12，虚拟环境位于 `.venv/`
- 由于系统级 `pip.ini`（`D:\conda\pip.ini`）存在编码问题导致 pip 全局报错，
  本项目使用**项目局部 pip 配置**绕过：`.venv/pip.ini` + 环境变量 `PIP_CONFIG_FILE`。
  **每次在新终端里用 pip 前，需先执行：**
  ```powershell
  $env:PIP_CONFIG_FILE = "e:\妙妙工具\炒股辅助\stock-quant-system\.venv\pip.ini"
  ```
- 依赖见 `requirements.txt`，按 Phase 分段注释。Phase 0（akshare / duckdb / pandas / numpy /
  requests / tqdm / PyYAML / python-dateutil / tushare）与 Phase 1（lightgbm / scikit-learn /
  scipy / statsmodels / pandas-ta / pyarrow）均已安装。
- 2026-08-25 起新增 `tushare`（Tushare Pro 2000积分档，200元/年，用户已付费升级）：token 存放
  在项目根目录 `.env`（`TUSHARE_TOKEN=...`，已在 `.gitignore`，不会入库/上传）。

## 2. 网络环境重要说明（务必阅读）

开发过程中实测发现：本机通过本地代理（Clash Verge, 127.0.0.1:7897）访问不同数据源的
稳定性差异很大：

| 数据源 | 稳定性 | 采用策略 |
|--------|--------|----------|
| 新浪财经（sina，如个股历史行情、财务指标接口） | **稳定**，作为主数据源 | `primary_source` |
| 东方财富（eastmoney，如 push2/push2his 系列接口） | **间歇性 502 / ProxyError / 连接重置**，具体原因未完全确定（可能是代理软件对该域名的路由规则、或该服务对代理出口IP的限流） | 仅作为北交所(bj)股票行情的 fallback，失败会被记录不阻塞主流程 |
| 上交所/深交所官方（`stock_info_a_code_name`） | 稳定 | 作为股票池主数据源 |

**已知影响**：北交所（bj，约338只股票，占全市场~6%）的日线行情目前依赖 eastmoney 接口，
在当前网络环境下大概率会持续失败。沪深（sh/sz，约94%的股票）不受影响。

**如果你想让北交所数据也能稳定抓取**，建议检查 Clash Verge 的分流规则，确认
`eastmoney.com` 及其子域名（尤其是 `push2his.eastmoney.com`）是否被正确路由为"直连"
而不是走代理节点（国内金融数据网站通过海外代理出口访问，容易被目标站点判定为异常流量）。
调整后重新运行 `python -m ingestion.quotes_batch` 即可自动补抓北交所数据（增量逻辑，
不会重复抓取已成功的部分）。

## 3. 已完成内容

### 3.1 目录结构

```text
stock-quant-system/
├── config.yaml                 # 全局配置：数据源优先级/重试策略/限速/股票池过滤规则
├── requirements.txt
├── common/
│   ├── config.py                # 配置加载
│   ├── db.py                    # DuckDB 连接与表结构（universe/daily_quotes/fundamentals/sync_log）
│   ├── http_retry.py            # 通用重试装饰器（指数退避+随机抖动）+ 限速sleep
│   ├── tushare_client.py        # Tushare共用客户端 + 逐股票/逐交易日通用批处理编排函数
│   └── ui_theme.py              # UI优化新增：全局CSS放大字体 + 建议卡片渲染 + 术语表(❓气泡)
├── ingestion/
│   ├── universe.py              # 股票池同步（代码/名称/交易所/板块/ST标记/退市标记）
│   ├── quotes_batch.py          # 全市场日线行情批量/增量抓取
│   ├── fundamentals_batch.py    # 全市场财务指标批量/增量抓取
│   ├── reference_data.py        # Phase 0.5：公告日历/退市股清单/行业分类
│   ├── market_data.py           # Phase 0.6轻量：交易日历/基准指数日线/指数成分快照
│   ├── corporate_actions.py     # Phase 0.6重量一：股本变动(市值基础)/分红配股(除权日)
│   ├── income_statement_batch.py# Phase 0.6重量二：利润表绝对值（营收/净利润等）
│   ├── tushare_prices.py        # 2026-08-25 Tushare：复权因子 + 退市股历史行情
│   ├── tushare_market_data.py   # 2026-08-25 Tushare：每日市值 + 指数历史成分 + 停复牌/涨跌停
│   └── tushare_behavior.py      # 2026-08-25 Tushare：Phase2行为金融原始数据预取
├── research/                    # Phase 1 研究层
│   ├── a_share_rules.py         # 涨跌停/成本规则（纯函数，不摸库）
│   ├── factors.py               # 量价+价值+质量因子定义（纯函数，不摸库）
│   ├── labeling.py               # 三重障碍标签（纯函数，不摸库）
│   ├── validation.py             # Purged K-Fold + Embargo、Walk-Forward 切分（纯函数）
│   ├── panel.py                  # point-in-time特征面板构建（唯一摸库的research模块）
│   ├── factor_eval.py            # 因子IC/RankIC/分层回测
│   ├── train_lightgbm.py         # 三重障碍标签+LightGBM LambdaRank训练
│   └── backtest.py               # 涨跌停/停牌/成本约束回测引擎
├── research/
│   └── train_dl.py               # Phase 3：行业图注意力深度学习挑战者（PyTorch）
├── advice/
│   ├── scanner.py                # 全市场每日打分排序输出+行为金融冲突提示+预测留痕
│   └── advice_engine.py          # Phase 4：建议卡片引擎（优先级：合规>风控>账户结构>信号>收益增强）
├── behavior/                      # Phase 2 行为金融代理指标（纯计算+摸库查询，不修改模型排名）
│   ├── chip_distribution.py      # 处置效应代理（VWAP成本近似）
│   ├── crowding.py               # 羊群效应/拥挤度（动量拥挤+龙虎榜频次+大单一致性）
│   └── sentiment_nlp.py          # 市场热度(涨跌停家数比)+大小单背离
├── risk/                          # Phase 4 风险与组合优化
│   ├── portfolio_risk.py         # 集中度/波动率/相关性矩阵/历史模拟法VaR-CVaR/回撤
│   └── portfolio_optimizer.py    # 均值-方差(Grinold-Kahn Alpha)/风险平价，Ledoit-Wolf协方差收缩
├── llm/                           # Phase 4 LLM解释层（翻译层，不做预测/建议本身）
│   ├── news_fetch.py             # 个股新闻抓取（东方财富，修复akshare已知regex bug）
│   └── explain_assistant.py      # DeepSeek API：把结构化信号+新闻标题翻译成自然语言解释
├── mlops/
│   ├── registry/<run_id>/        # model.pkl + metadata.json，每次训练一份
│   ├── registry/champion.json    # 当前冠军模型指针
│   ├── registry/promotion_log.jsonl  # 模型版本演进历史（每次晋升决策留痕）
│   ├── retrain_schedule.py       # 定期重训 + 冠军-挑战者晋升门禁(CD4ML)
│   └── drift_monitor.py          # 特征PSI + Walk-Forward趋势 + 生产样本外IC + 滚动夏普
├── scripts/
│   └── daily_pipeline.py        # 全ingestion模块统一增量更新入口（唯一定时任务入口）
├── app.py                        # Phase 4：Streamlit入口——持仓驾驶舱总览 + 今日决策速览
├── launcher.py                   # UI优化：一键启动器源码（打包为 研衡启动器.exe）
├── .streamlit/config.toml        # UI优化：全局主题配色
├── pages/                        # Streamlit多页应用
│   ├── 1_持仓与建议.py            # 手动持仓录入 + 建议卡片（含组合优化再平衡覆盖层+具体价格）
│   ├── 2_掘金扫描.py              # 全市场模型排名 + 行为金融冲突提示
│   ├── 3_风险仪表盘.py            # 集中度/波动率/相关性/VaR/回撤可视化
│   ├── 4_AI解释.py               # 结构化信号+新闻 -> 通义千问自然语言解释
│   ├── 5_行情图表.py              # UI优化新增：K线 + 持仓成本/止盈止损参考线
│   ├── 6_名词解释.py              # UI优化新增：RankIC/VaR/因子等大白话术语表
│   └── 7_历史建议复盘.py          # UI优化新增：过去建议 vs 后续实际涨跌幅核对
├── data/
│   ├── warehouse.duckdb         # 数据仓库（不入库，见 .gitignore）
│   ├── feature_panel.parquet    # research/panel.py 产出的月度特征面板缓存
│   └── scans/                    # advice/scanner.py 每次运行的候选清单CSV
├── .env                          # TUSHARE_TOKEN + DASHSCOPE_API_KEY（不入库，见 .gitignore）
└── logs/
    └── _archive_phase0-0.7/      # Phase 0-0.7 抓取过程日志归档
```

### 3.2 数据表结构（DuckDB）

- `universe`：全市场股票基础信息，字段含 `exchange`(sh/sz/bj)、`board`(main/gem/star/bse)、
  `is_st`、`is_delisted`（本次同步快照中缺失的股票会被标记疑似退市/停牌超期）、
  `first_seen_date`/`last_seen_date`。
- `daily_quotes`：前复权日线 OHLCV + 成交额/换手率/涨跌幅，主键 `(symbol, trade_date, adjust)`，
  支持增量更新（只补抓缺失日期）。
- `fundamentals`：财务指标，**EAV长表设计**（`symbol, report_date, indicator, value`），
  因为新浪接口单只股票返回 80+ 项指标且指标集合可能演进，固定列会导致频繁改表；
  Phase 1 做因子研究时用 `PIVOT` 展开成宽表。**已知限制**：该接口只提供报告期，
  没有公告日，用于回测前需要额外处理 point-in-time 对齐（见 `common/db.py` 表注释）。
- `sync_log`：每次抓取任务的成功/失败日志，用于审计和失败重试队列。
- `prediction_log`（Phase 2新增）：每次 `advice/scanner.py` 运行的全量打分留痕
  （`symbol, trade_date, model_run_id, pred_score, rank, is_tradable`），供
  `mlops/drift_monitor.py` 做生产环境样本外IC监控，也是建议可追溯性的数据基础。
- `positions`（Phase 4新增）：手动持仓录入（本项目不接券商API），支持分批建仓(`lot_id`)，
  Streamlit「持仓与建议」页面读写。
- `advice_log`（Phase 4新增）：每次 `advice/advice_engine.py` 生成的建议卡片留痕
  （含理由/风险/失效条件JSON + 对应`model_run_id`），任意建议可追溯回放。
- **Phase 0.5/0.6 新增表**（详见对应章节与 `common/db.py` 表注释）：`disclosure_calendar`
  （公告日历，point-in-time）、`industry_classification`（申万行业分类变动历史）、
  `trade_calendar`（交易日历）、`index_quotes`（基准指数日线）、`index_constituents`
  （指数成分快照）、`share_changes`（股本变动，市值基础）、`dividends`（分红配股，除权日）、
  `income_statement`（利润表绝对值，EAV设计同`fundamentals`）。
- **2026-08-25 Tushare 新增表**：`adj_factor`（复权因子）、`daily_basic`（官方每日市值/PE/PB/PS
  时间序列）、`index_weight`（指数历史成分，point-in-time月度快照）、`suspend_calendar`（停复牌
  历史）、`limit_price`（官方每日涨跌停价格）；Phase 2 预取表：`dragon_tiger_list`（龙虎榜）、
  `block_trade`（大宗交易）、`margin_balance`（融资融券汇总）、`moneyflow`（个股资金流向）、
  `pledge_stat`（股权质押统计）、`holder_number`（股东人数）。

### 3.3 抓取健壮性设计

- `common/http_retry.py`：指数退避重试（默认5次，最长等60秒）+ 随机抖动，避免"重试风暴"。
- 每只股票的请求之间随机休眠 0.35~0.8 秒（`polite_sleep`），降低触发数据源反爬限制的概率。
- 增量更新基于"该股票在库里的最大日期"计算起始点，脚本随时可中断、重新运行，
  天然支持断点续传，不会重复抓取。
- 单只股票抓取失败不会中断整批任务，失败记录写入 `sync_log`，下次运行会自动重试
  （因为该股票的"最大日期"没有前进）。

## 4. 已验证结果（2026-08-26 实测，非抽样估算）

详细数字与逐项缺口见 `docs/phase0-acceptance-report.md`，摘要：

- `universe`：5910只（活跃5549 / 退市361），行业/板块分类100%覆盖。
- `daily_quotes`（qfq）：**5549/5549** 活跃股票全覆盖（含北交所338只+CDR 689009），1121万行。
- `adj_factor` 99.54%、`daily_basic` ~100%、`income_statement` 100%（活跃股票）。
- `income_statement`×`fundamentals` 交叉覆盖率 99.26%；`fundamentals` 北交所缺口已用
  Tushare `fina_indicator` 补齐338/338。
- `share_changes` 覆盖率91.08%（5054/5549），已查明并修复 akshare 库自身的 KeyError 缺陷，
  剩余缺口是数据源真实无记录（338北交所接口不支持+157只无历史股本变动事件），非bug。
- `moneyflow_hsgt`：2742个交易日完整覆盖（2015-01-05~2026-08-25）。

## 5. 如何运行

```powershell
$env:PIP_CONFIG_FILE = "e:\妙妙工具\炒股辅助\stock-quant-system\.venv\pip.ini"
$venv = "e:\妙妙工具\炒股辅助\stock-quant-system\.venv\Scripts\python.exe"
cd "e:\妙妙工具\炒股辅助\stock-quant-system"

# ---------- 数据层：日常/每周增量更新（唯一入口） ----------
& $venv -m scripts.daily_pipeline
& $venv -m scripts.daily_pipeline --only quotes,tushare_prices   # 只跑指定步骤
& $venv -m scripts.daily_pipeline --skip fundamentals,income_statement  # 跳过更新频率低的步骤

# ---------- 研究层：因子面板 -> 因子评估 -> 训练 -> 回测 -> 扫描 ----------
& $venv -m research.panel --start 20160101              # 重建point-in-time特征面板
& $venv -m research.factor_eval                          # 因子IC/RankIC/分层回测报告
& $venv -m research.train_lightgbm                       # 训练LightGBM排序模型，写入mlops/registry
& $venv -m research.backtest                             # 涨跌停/停牌/成本约束回测
& $venv -m advice.scanner --top-n 50                      # 今日全市场候选清单，写入data/scans/

# ---------- 模型生命周期(Phase2)：重训调度 -> 漂移监控 ----------
& $venv -m mlops.retrain_schedule --bootstrap             # 首次：把已有模型设为初始冠军(只需一次)
& $venv -m mlops.retrain_schedule                          # 每周末跑一次：重训挑战者+冠军对比晋升
& $venv -m mlops.drift_monitor                             # 特征PSI/Walk-Forward趋势/滚动夏普看板

# 验收查询示例：全市场条件筛选
& $venv -c "from common.db import get_connection; conn = get_connection(); print(conn.execute(\"SELECT count(*) FROM universe WHERE is_st=FALSE AND is_delisted=FALSE\").fetchall())"

# ---------- 应用层(Phase4)：本地Streamlit桌面应用 ----------
& $venv -m streamlit run app.py     # 启动后浏览器访问 http://localhost:8501
# 日常使用推荐直接双击项目根目录「研衡启动器.exe」，效果等价于上面这条命令(见第12节)
# AI解释页需要在 .env 里配置 DASHSCOPE_API_KEY=xxx（阿里云百炼 https://bailian.console.aliyun.com/）
# 未配置时该页面自动降级为仅展示结构化数据，其余页面不受影响
```

## 6. Phase 0 执行结果与已知限制

- 全市场行情：**5549/5549**（100%）活跃股票已覆盖，覆盖2015-01-05~2026-08-25，1121万行。
  此前缺口的338只北交所股票+1只科创板CDR股（689009）已于2026-08-26用 Tushare `daily`
  （qfq，配合 `adj_factor`）补齐，不再依赖当时不稳定的东方财富接口。
- 全市场财务指标：**过程中发现并修复一个 schema 漂移bug**：
  主库里 `fundamentals` 表长期停留在迁移前的旧版固定列结构（因为 `CREATE TABLE
  IF NOT EXISTS` 不会修改已存在的表），因为之前所有测试都在隔离测试库上做、从未真正
  在主库跑过全市场任务，所以一直没暴露。该表当时为空，已安全DROP重建为设计中的
  EAV结构，无数据丢失。

## 7. Phase 0.5（数据缺口补齐）—— 代码已完成，待主库锁释放后执行

基于行业对标（Qlib / López de Prado / Feast）做必要性+可实现性评估后确定的三项，
详见 `docs/phase0.5-gap-closing-assessment.md`，均已开发完成并在隔离测试库验证通过：

- **财务公告日历**（`ingestion/reference_data.py::sync_disclosure_calendar`）：解决"用未公告
  数据训练/回测"的未来函数风险，抽样测试32740条记录0失败
- **退市股清单**（`sync_delisted_universe`）：361只沪深退市股准确上市/退市日期回填 `universe`，
  缓解幸存者偏差（价格历史仍是已知缺口，见评估文档）
- **申万行业分类变动历史**（`sync_industry_classification`）：12903条记录覆盖5912只股票，
  天然point-in-time，供 Phase 1 行业中性化使用

因为 DuckDB 单文件同一时刻只能一个进程写入，这三项要等财务批量抓取任务跑完释放主库锁后
才能对 `warehouse.duckdb` 正式执行（预计几分钟内完成）。

## 7.5 Phase 0.6（数据缺口补齐第二轮）—— 代码已完成，自动编排脚本已在后台运行

用户对"当前数据是否覆盖行业标准所需信息"提出质疑后，做的第二轮系统性评估，详见
`docs/phase0.6-secondary-gap-assessment.md`。逐项真实请求验证后通过的部分：

- **交易日历 + 基准指数日线 + 指数成分快照**（`ingestion/market_data.py`，轻量，分钟级）：
  `tool_trade_date_hist_sina`（8797个交易日）、`stock_zh_index_daily`（新浪源，沪深300/中证500/
  中证1000/上证指数，东财源`index_zh_a_hist`实测ProxyError已放弃）、`index_stock_cons_csindex`
  （中证官方源，三大指数当前成分共1800条）
- **股本变动历史 + 分红配股历史**（`ingestion/corporate_actions.py`，逐股票，3-5小时）：
  `stock_share_change_cninfo`（总股本/流通股本+公告日期，市值/规模因子的基础输入，当前完全
  缺失的一项）、`stock_dividend_cninfo`（送股/转增/派息比例+除权日，避免除权日误判涨跌停）
- **利润表绝对值**（`ingestion/income_statement_batch.py`，逐股票，3-5小时）：
  `stock_financial_report_sina(symbol="利润表")`，补齐 `fundamentals` 表只有比率无绝对金额的
  缺口（营业总收入/营业利润/净利润等约80项），支撑PS估值和同行业体量对比

三个模块均已在隔离测试库验证通过（`market_data`: 8797+22826+1800条；`corporate_actions`:
2只测试股票79条股本变动+59条分红；`income_statement_batch`: 2只测试股票6619条）。

**已写好全自动编排脚本 `scripts/auto_chain_runner.py` 并已在后台启动运行**：轮询财务指标
批量任务（`ingestion.fundamentals_batch`）进程是否结束（用 `psutil` 检测进程存活，而非看
日志是否更新，避免"慢速重试期间长时间不更新日志"导致误判），任务结束后自动依次对主库执行
`reference_data`（Phase 0.5）→ `market_data` → `corporate_actions` → `income_statement_batch`，
全程无需人工干预，每步日志见 `logs/<task>.auto.log`，整体进度见 `logs/auto_chain_runner.log`。
（`auto_chain_runner.py` 是一次性编排脚本，Phase 0-0.7 全部完成后已删除，日常增量更新已由
`scripts/daily_pipeline.py` 统一替代，见第3.1节目录结构与第5节运行方式。）

~~明确记录为已知缺口、本轮不做~~：以下缺口已于2026-08-25用Tushare Pro 2000积分档全部关闭，
详见第7.6节：未复权价格全历史/复权因子、历史指数成分股变更、精确停牌区间、龙虎榜/资金流/
融资融券/股权质押/股东人数。仍未关闭：北向资金明细（`moneyflow_hsgt`未验证）、宏观经济数据
（维持排在 Phase 4，高频/做市类功能仍不在设计范围内）。

## 7.6 Tushare Pro 集成（2026-08-25）—— 用户付费升级后彻底关闭剩余数据缺口

用户批准将 Tushare 账户升级到 **2000积分档（200元/年）**，限频从"部分接口1次/小时"提升到
"200次/分钟、100000次/天/接口"。升级前先用探测脚本实测确认解锁范围（非假设），确认后正式
开发三个模块，均已在隔离测试库用**真实API请求**（非mock）验证：

- **`ingestion/tushare_prices.py`**：`sync_adj_factor()`（复权因子，逐股票含退市股，解决"未
  复权价格"缺口）+ `sync_delisted_daily_quotes()`（退市股历史行情，此前akshare/sina/eastmoney
  三个源全部失败，是全项目最大的幸存者偏差缺口，本次用Tushare的`daily`接口直接补齐）
- **`ingestion/tushare_market_data.py`**：`sync_daily_basic()`（官方每日市值/PE/PB/PS时间序列，
  比自建股本变动拼接更权威）+ `sync_index_weight()`（指数历史成分point-in-time月度快照，首次
  真正解决"某股票在T日是否属于沪深300"这类历史判断）+ `sync_suspend_calendar()`（停复牌历史）
  + `sync_limit_price()`（官方每日涨跌停价格，可与`research/a_share_rules.py`自算规则交叉校验）
- **`ingestion/tushare_behavior.py`**：Phase 2 行为金融层原始数据提前预取（不代表提前开工建模）
  ——龙虎榜、大宗交易、融资融券汇总、个股资金流向、股权质押统计、股东人数，全部对应用户明确
  提到的"大额买入同时小额卖出"这类博弈论/资金流场景的原始数据

共用基础设施 `common/tushare_client.py`：统一 `pro_api()` 客户端（token 从 `.env` 读取）+ 两种
通用批处理编排（逐股票循环 / 逐交易日市场快照循环），避免每个具体任务重复写循环+重试+断点
续传逻辑。已接入 `scripts/auto_chain_runner.py` 自动执行链的末尾三步，全程无需人工干预。

**处理运维事故记录**：升级Tushare后重启 `auto_chain_runner.py` 时，误将持续运行的
`fundamentals_batch`（当时进度26%）连带终止（Windows venv `python.exe` 是"父进程会话"，
其下的真实解释器进程是子进程，杀父进程会级联杀子进程，两者需当作同一个整体处理，不能只杀
"看起来空闲"的那个）。经代码确认 `fundamentals_batch` 按 `symbol` 级别做增量续传（记录已有
`max(report_date)`，重启后已完成的symbol只需重新确认"无新数据"即快速跳过，重新拉取的），
重启后无数据丢失，只多花约1小时重新扫过已完成的symbol，随后恢复正常抓取速度。

## 8. Phase 1 —— 因子研究 + LightGBM排序模型 + 全市场扫描（2026-08-26 首轮完成）

详细方法论、逐因子IC表、模型样本外表现、回测结果见 `docs/phase1-acceptance-report.md`。
本节只列代码结构与如何复现：

- `research/a_share_rules.py` / `labeling.py` / `validation.py`：涨跌幅/次新过滤/交易成本、
  三重障碍标签、Purged K-Fold+Embargo/Walk-Forward切分——均为纯函数，不摸库，
  单测：`python -m scripts.test_research_offline`
- `research/factors.py`：量价因子（MOM-12-1/REV-1M/波动率/换手/振幅）+ 新增价值因子
  （BP/EP/SP/对数市值）+ 质量因子（ROE/毛利率/低杠杆/净利润增速），全部为纯函数
- `research/panel.py`：**唯一摸库的research模块**，把量价/价值/质量因子接到真实数据，
  用 DuckDB ASOF JOIN 做质量因子的 point-in-time 对齐（disclosure_calendar公告日，
  缺失时用季报T+45/年报T+60兜底），月度截面，10年历史约52万行、构建耗时约10秒
- `research/factor_eval.py`：13个因子的IC/RankIC/分层多空回测，`factor_bp`（价值）表现最好
  （RankIC均值0.065），验证了A股价值因子的经典结论
- `research/train_lightgbm.py`：LightGBM LambdaRank排序模型，Purged K-Fold与Walk-Forward
  双验证，样本外RankIC均为正（walk-forward约0.06，更贴近真实可部署表现）
- `research/backtest.py`：接入官方 `limit_price`/`suspend_calendar` 做入场可交易性过滤，
  叠加 `a_share_rules` 的佣金/印花税/过户费/滑点成本，产出扣费前后两套业绩指标，
  并与"全市场等权平均"基准对比（避免把"赶上了小盘股普涨"误判成模型能力）
- `advice/scanner.py`：加载最新模型 + 当前市场快照，输出全市场排序候选清单
  （`data/scans/`），已排除ST/停牌/涨停封死不可买入的标的
- `mlops/registry/<run_id>/`：每次训练的 `model.pkl` + `metadata.json`（样本外指标+特征列
  +训练时间），简单版本化，完整的漂移监控/自动重训留给 Phase 2

**已知局限（如实记录，避免过度自信）**：universe.is_st只是当前标记非历史时点标记；
回测的出场日可成交性按市值重估简化处理（非逐笔撮合）；模型当前对北交所/小微盘有明显偏好，
真实部署前需要补充行业/规模集中度约束；样本外回测只有单条walk-forward路径、84个月度观测，
统计显著性需要更长历史/更多验证路径进一步确认。

## 9. Phase 2 —— 模型生命周期管理 + 行为金融信号层（2026-08-26 完成）

详细结果、真实数字、发现并修复的一个数据bug见 `docs/phase2-acceptance-report.md`。
本节只列代码结构与如何复现：

- `mlops/retrain_schedule.py`：冠军-挑战者晋升门禁（CD4ML模式）——重建面板 -> 训练挑战者 ->
  同一套Walk-Forward协议样本外RankIC与当前冠军比较 -> 晋升或保留，决策写入
  `mlops/registry/promotion_log.jsonl`（模型版本演进历史）。已完成一次bootstrap
  （把Phase1模型设为初始冠军）。
- `mlops/drift_monitor.py`：特征PSI（训练基线 vs 当前快照）+ Walk-Forward历史趋势 +
  生产环境样本外IC（`prediction_log`累积够数据后才出结论）+ 回测期滚动夏普。
  2026-08-26首次运行发现`factor_rev_1m`/`factor_mom_12_1`两个动量类因子显著漂移
  （PSI 1.32/0.87），与同期市场动量拥挤度极端值、回测期滚动夏普持续恶化互相印证，
  指向"当前处于一次风格切换"这一个合理解释，不是孤立的异常。
- `behavior/`：处置效应（`chip_distribution.py`，VWAP成本代理）、羊群效应/拥挤度
  （`crowding.py`，动量拥挤+龙虎榜频次+大单一致性）、市场热度+大小单背离
  （`sentiment_nlp.py`）三个代理指标模块，全部用已有真实数据计算，如实标注了"不是官方
  逐日筹码分布"“不含真正NLP文本情绪"这两处范围局限。
- `advice/scanner.py`：Top候选叠加行为金融信号产出`conflict_flag`列（交叉验证提示，
  不改变模型排名），并把每次全量打分写入新表`prediction_log`（建议可追溯性 + 漂移监控
  数据基础）。

**已知局限（如实记录）**：生产环境样本外漂移曲线需要几周`prediction_log`累积才有统计意义；
行为金融情绪层暂不含真正的新闻NLP（等Phase 4资讯管道）；筹码分布是量价代理不是官方数据
（官方接口当前账户权限不覆盖）。

## 10. Phase 3 —— 深度学习挑战者评估（2026-08-26 完成，结论：维持LightGBM基线）

详细数字、统计检验、如实记录的矛盾发现见 `docs/phase3-acceptance-report.md`。结论摘要：

- `research/train_dl.py`：实现并训练了行业关系图注意力模型（简化GAT，块对角掩码 +
  `scaled_dot_product_attention`），用与LightGBM完全相同的Walk-Forward协议评估。
- **walk-forward样本外RankIC：DL(0.1112) 统计显著优于 LightGBM(0.0608)**（配对t检验
  p=0.0182，7折全部占优）。
- 但代入完全相同的Top-30组合回测后，**DL的净收益(CAGR 24.5%, Sharpe 0.90)反而不如
  LightGBM(CAGR 54.7%, Sharpe 1.31)**，两模型月度净收益配对t检验 p=0.0606（方向不利于DL）。
- 按计划文档Phase3验收条款"深度学习模型的样本外IC/夏普必须优于LightGBM基线，否则维持基线
  为生产模型"——**IC更优但Sharpe/收益不如基线，不满足"优于"的完整条件，维持LightGBM为
  生产冠军**，决策已记录进 `mlops/registry/promotion_log.jsonl`。DL模型文件保留在
  registry中留痕，未删除。

**已知局限/后续方向（如实记录）**：全市场排序能力提升未转化为Top-N选股收益提升的具体原因
未逐一验证（假设：pairwise loss未对Top分位加权、行业内注意力可能抹平了组内个股差异），
留作后续如果继续打磨DL方向的首选切入点，不在本轮范围内展开。

## 11. Phase 4 —— 持仓驾驶舱 / 场景化建议引擎 / 组合优化 / 风险仪表盘 / LLM解释层（2026-08-26 完成）

详细验收记录、UI截图验证过程、已知局限见 `docs/phase4-acceptance-report.md`。本节列代码
结构与如何复现：

- `risk/portfolio_risk.py`：手动持仓的集中度、逐股票年化波动率、历史模拟法VaR/CVaR、
  持仓相关性矩阵、按当前权重重放历史的假设性回撤。
- `risk/portfolio_optimizer.py`：补齐`docs/00-总览/02-产品定位与边界.md` Should(V1)条款
  "组合优化（约束均值方差/风险平价简化版）"——均值-方差用Grinold-Kahn精化Alpha
  (`alpha=IC×σ×score_z`)结合`sklearn.covariance.LedoitWolf`协方差收缩，风险平价用等风险
  贡献目标，两者都用`scipy.optimize`的SLSQP求解，单票集中度上限约束与`portfolio_risk.py`
  共用同一阈值。**如实标注**：这是Phase4验收时对照产品边界文档才发现的缺口，原Phase2-4
  任务列表未单列，属于补齐动作，不含行业/风格中性化等机构级约束。
- `advice/advice_engine.py`：建议卡片引擎，严格按"合规可交易性 > 生存风控(止损/集中度) >
  账户结构 > 信号质量 > 收益增强(再平衡)"优先级产出`watch/open/hold/reduce/stop_loss/
  take_profit/rebalance`七种信息性建议（不自动下单）。`apply_rebalance_overlay()`只会把
  已判定为`hold`（更高优先级都未触发）的卡片，在组合优化目标权重偏离超过3%时升级为
  `rebalance`，不会覆盖任何风控结论。止盈止损阈值复用生产冠军模型训练时的三重障碍标签
  配置（8%/8%/20日），逻辑自洽。
- `llm/news_fetch.py` + `llm/explain_assistant.py`：个股新闻抓取（修复了akshare
  `stock_news_em`在本环境下的pyarrow正则bug）+ DeepSeek API"翻译层"——只做自然语言解释，
  强制规则禁止LLM给出买卖指令性表述，未配置`DASHSCOPE_API_KEY`时优雅降级。
- `app.py` + `pages/`：Streamlit 4页应用（总览/持仓与建议/掘金扫描/风险仪表盘/AI解释），
  已用Playwright浏览器自动化逐页验证真实交互（录入持仓->生成建议卡片->触发集中度超限
  "减仓"建议->AI解释页正确降级提示缺少API Key）。

**已知局限（如实记录）**：不接券商API，持仓为手动录入，无法反映真实交易滑点/费率差异；
组合优化不含行业/风格中性化约束，assumed_IC=0.03是保守拍定值非逐日重估；LLM解释层依赖
用户自行配置API Key，属于外部凭据缺口不是本系统能自解的。

## 12. 可用性测试通过后的UI/UX优化 + 一键启动打包（2026-08-26）

人工可用性测试通过后，针对"字体偏小、缺少直观易懂的功能"两点反馈做的迭代，完整决策记录/
测试过程见 `docs/ux-upgrade-notes.md`，代码层面：

- `common/ui_theme.py` + `.streamlit/config.toml`：全局CSS放大正文/指标/表格/按钮字号，
  统一配色主题，所有页面从`apply_theme()`一个函数入口接入，不在各页面重复写样式。
- 建议卡片改版（`advice/advice_engine.py` 新增 `price_levels()`/`_plain_summary()`）：
  把止盈止损百分比换算成具体人民币价格，并生成一句大白话总结，`advice_log`表新增
  `name`/`plain_summary`/`price_levels_json`列做持久化。
- 首页新增"今日决策速览"（`build_priority_digest()`）：读`advice_log`最近一次结果，
  按"止损>减仓>止盈>再平衡>建仓>持有>观望"优先级排序展示，不用逐页翻找。
- 新增3个页面：`5_行情图表.py`（K线+持仓成本/止盈止损参考线）、`6_名词解释.py`
  （RankIC/VaR/因子等大白话术语表，与各页面❓气泡共享同一份术语数据）、
  `7_历史建议复盘.py`（核对过去建议后续实际涨跌幅，如实标注"非严格回测"）。
- `launcher.py` + `scripts/build_exe.py`：一键启动打包为`研衡启动器.exe`——只把启动逻辑
  打包，不把torch/lightgbm/duckdb整体冻结进exe（工程取舍原因见开发者说明书第9a节），
  已实测双击exe能正确拉起Streamlit并自动打开浏览器。

**已知局限（如实记录）**：`历史建议复盘`是简化统计（发出日收盘价->最新收盘价涨跌幅），
不是考虑滑点/仓位/组合效应的严格回测；exe本身不含Python依赖，仍需预先搭建好`.venv`，
换新机器分发时需要先完成一次性环境安装，这是明确的设计取舍不是遗漏。

## 13. 对照UI/UX设计理论库的第二轮复核 + 全流程边界测试（2026-08-26）

对照独立设计理论库（认知负荷/Gestalt/信息图示/无障碍对比度/栅格/色彩Token/字体层级等13个
分册）逐条核对第12节的UI实现，只改有实测证据支撑的问题，完整对照表、WCAG对比度实测数据、
边界测试清单见 `docs/ux-upgrade-notes.md` 第6-7节。要点：

- 用WCAG相对亮度公式实测发现"减仓"/"止盈"两个操作色对比度不达标（3.46:1/4.56:1贴线），
  已调深到`#BF360C`/`#1B5E20`（5.11:1/7.00:1），其余5种操作色实测本来就达标。
- 字号阶梯改成以正文18px为基准、公比1.25(Major Third)的Modular Scale，四级标题第一次有
  统一可解释的比例关系（此前是四个随手取的数字）。
- 持仓集中度图由饼图改为水平柱图（Tufte/Few"饼图超4片应改柱图"规则，direct label优于图例）。
- 新增`common/ui_theme.py::section_header()`统一"标题+❓帮助按钮"排布，替换3处页面里各自
  手写的`st.columns([5,1])`。
- 全流程边界测试覆盖表单空值/非法股票代码/搜索无匹配/筛选器清空等7类场景，均已验证优雅
  降级；顺带发现并修复一个真实bug：`历史建议复盘`页对pandas `NaN`用`or ''`兜底无效
  （`NaN`在Python里是真值），导致缺少name的旧记录显示成字面"nan"，已用`pd.notna()`修复。
