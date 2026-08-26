# Phase 0.6 数据缺口补齐（第二轮）—— 必要性与可实现性评估

> 背景：用户对"当前抓取的数据是否覆盖行业标准所需的全部信息"提出质疑。上一轮回复中列出的
> "缺了会明显伤模型"清单（总股本/总市值、指数日线、利润表绝对值等）在本文档中逐项做真实
> 请求验证（而非停留在"理论上有这个接口"），只有必要性和可实现性都通过才排入自动化执行队列。
> 评估时间：2026-08-25，在 Phase 0 财务指标批量抓取任务运行期间用小规模探测请求完成，探测脚本
> 均写入隔离位置且已清理，未触碰主库文件锁。

## 1. 评估方法（沿用 Phase 0.5 标准）

- **必要性分级**：高（不修复 = 因子/回测结论可能是假的或严重失真）/ 中（削弱专业度但不致命）/ 低（锦上添花）
- **可实现性分级**：高（已用真实请求验证）/ 部分 / 不可行（未找到可靠免费源）
- **成本分级**（本轮新增，因为部分项目虽然可行但是"逐股票循环"而非"全市场一次请求"，两者耗时差 2-3 个数量级）：
  - 轻量：≤ 10 次请求，覆盖全市场，分钟级
  - 重量：约 5000+ 次请求（逐股票），小时级，需要像 quotes_batch/fundamentals_batch 一样排队跑
- 只有 **必要性=高 且 可实现性=高或部分** 才纳入执行范围；重量级项目会按"零成本但占时间"的原则排队自动执行，不因为耗时长而放弃，但会明确告知预期时长。

## 2. 逐项评估结果

| # | 项目 | 必要性 | 可实现性 | 成本 | 验证方式与结论 | 决策 |
|---|------|--------|----------|------|-----------------|------|
| M | A股交易日历 | 高：因子计算里"滚动N个交易日"窗口、T+N标签、节假日跳空全部依赖准确交易日历，用自然日近似会系统性引入噪声 | **高** | 轻量（1次请求） | `ak.tool_trade_date_hist_sina()` 实测成功，返回1990-12-19~2026-12-31共8797个交易日 | **通过，纳入 Phase 0.6** |
| N | 主要基准指数日线（沪深300/中证500/中证1000/上证指数） | 高：Beta、超额收益、行业/市场中性化、"大盘跌但个股涨"这类用户明确提到的场景判断，全部需要基准指数做对照，没有基准指数所有"相对强弱"判断都无法量化 | **高**（sina源） | 轻量（4次请求，每次返回该指数全历史） | `ak.index_zh_a_hist`（东财源）实测 `ProxyError` 失败；换用 `ak.stock_zh_index_daily(symbol="sh000300"/"sh000905"/"sh000852"/"sh000001")`（新浪源）全部成功，沪深300 5977条/中证500 5256条/中证1000 2883条/上证指数8710条 | **通过，纳入 Phase 0.6**（东财源不可用，改用新浪源） |
| O | 总股本/流通股本变动历史（用于计算总市值/流通市值） | 高：市值是规模因子(Size)、PS估值、流动性归一化、组合权重分配的基础输入，当前完全缺失；PE/PB虽已有比率但缺市值就无法做"多少市值对应多少利润"这类绝对量分析 | **高** | **重量**（逐股票，约5500次请求） | `ak.stock_share_change_cninfo(symbol="000001")` 实测成功，返回45条历史记录，含总股本/已流通股份/变动日期/公告日期(point-in-time)，巨潮资讯官方源，与已验证稳定的 disclosure_calendar 同源 | **通过，纳入 Phase 0.6**，按 fundamentals_batch 同样的逐股票批处理模式排队执行；**2026-08-25追加**：Tushare `daily_basic` 提供交易所官方计算的**每日**总市值/流通市值/PE/PB/PS时间序列（不是变动事件点，是逐日快照），比本项"股本变动事件+收盘价"手动拼接更权威更省算力，见第7节 `sync_daily_basic()`，两者并存互相校验 |
| P | 分红配股历史（送股/转增/派息比例、除权日） | 高：①不知道除权日就无法准确解释"复权价格在那天为什么突然大幅变化"，容易被误判为涨跌停或异常波动；②理论库有"打新/分红"相关场景，且除权日是唯一能验证前复权价格计算是否正确的外部基准 | **高** | **重量**（逐股票，约5500次请求） | `ak.stock_dividend_cninfo(symbol="000001")` 实测成功，返回28条记录，含送股比例/转增比例/派息比例/股权登记日/除权日/派息日，巨潮官方源 | **通过，纳入 Phase 0.6**，与项O合并成一个逐股票批处理任务（同一批次里顺带抓取，减少总循环次数） |
| Q | 利润表绝对值（营业总收入/营业利润/净利润等绝对金额） | 中高：当前 `fundamentals` 表只有比率型指标（ROE/EPS等），缺绝对金额会导致 PS 估值（市销率）、"同行业公司体量对比"、营收增速的绝对值校验都做不了 | **高** | **重量**（逐股票，约5500次请求，且返回体量大于财务指标接口） | `ak.stock_financial_report_sina(stock="sh600600", symbol="利润表")` 源码确认为新浪源、EAV友好格式（item_title/item_value），与已用的 `stock_financial_report_sina` 系列同源，风险与现有 fundamentals 抓取一致 | **通过，纳入 Phase 0.6**，但排在O/P之后（优先级略低于市值数据） |
| R | 历史指数成分股变更（point-in-time 成分股名单） | 中：影响"某股票在T日是否属于沪深300"这类历史判断的精确性，但当前项目不做指数增强/被动跟踪类策略，暴露面有限 | ~~部分~~ → **已于2026-08-25用Tushare彻底关闭** | 轻量（3指数×约240个月末交易日≈720次请求） | Tushare `index_weight(index_code, trade_date=月末交易日)` 实测：2024-01-31返回沪深300全部300条成分+权重；确认是"月度快照"机制（自然月末非交易日如春节假期会返回空表，需按 trade_calendar 取真实最后交易日） | **升级为完全通过**，见第7节，`ingestion/tushare_market_data.py::sync_index_weight()` |
| S | 未复权（原始）收盘价全历史 | 中：目前只有前复权(qfq)价格；qfq 在两个相邻交易日之间的涨跌幅比例本身是准确的，**唯一失真的场景是除权日当天**（除权日的"除权价"计算公式与普通涨跌停公式不同），会被简单规则误判涨跌停锁定状态 | ~~高但成本=重跑一次全量~~ → **已于2026-08-25用Tushare关闭，成本降至逐股票循环量级** | 重量（逐股票，约5500次请求，与fundamentals_batch同量级，非"二次全量" ） | Tushare `adj_factor(ts_code)` 一次调用返回该股票全部历史复权因子（实测000001.SZ返回6000条），可直接用 `qfq_price = raw_price * adj_factor(t)/adj_factor(最新)` 换算，不需要重新抓一遍未复权行情 | **升级为完全通过**，见第7节，`ingestion/tushare_prices.py::sync_adj_factor()` |
| T | 停牌/复牌历史区间 | 中：影响"该股票在T日是否可交易"的精确性 | ~~不可行~~ → **已于2026-08-25用Tushare关闭** | 轻量（逐交易日，约2700次请求，覆盖全市场每次） | Tushare `suspend_d(trade_date)` 实测2024-03-01返回10条停牌记录（含suspend_type，S=停牌），官方源，市场快照式调用，比逐股票循环快得多 | **升级为完全通过**，见第7节，`ingestion/tushare_market_data.py::sync_suspend_calendar()` |
| U | 北向资金/龙虎榜/融资融券余额/十大股东/机构持仓 | 中：博弈论/资金流场景（用户明确提到"大额买入同时小额卖出"）的原料，但当前 Phase 1 目标是先跑通量价+基础财务因子的 LightGBM 基线，这类数据属于加分项而非基线必需 | ~~待验证~~ → **已于2026-08-25用Tushare验证全部可用** | 混合（top_list/block_trade/margin逐交易日；moneyflow/pledge_stat/stk_holdernumber逐股票） | Tushare `top_list`(龙虎榜)/`block_trade`(大宗交易)/`margin`(融资融券)/`moneyflow`(资金流向)/`pledge_stat`(股权质押)/`stk_holdernumber`(股东人数) 六个接口全部实测成功且返回结构清晰 | **数据源验证通过，原始数据提前抓取落地**（见第7节），但**建模排期不提前**——仍在 Phase 2 才开工做行为金融代理信号计算，本轮只是让数据"等模型不等抓取" |
| V | 宏观经济数据（利率/PMI/社融） | 中：美林时钟/资产配置类场景的原料，但当前系统定位是"个股选股+风险管理"，宏观是 Phase 4 组合层的输入 | 待验证 | 轻量（预估） | 维持原计划 Phase 4 | **维持 Phase 4，不提前** |

## 3. Phase 0.6 正式开工范围（评估通过项，按执行顺序）

在 Phase 0 财务指标批量抓取任务跑完释放主库文件锁后，**自动依次执行**（已写好编排脚本，见第5节）：

1. **轻量批次**（预计 5 分钟内，`ingestion/market_data.py`）
   - `sync_trade_calendar()`：全市场交易日历 → 新表 `trade_calendar(trade_date)`
   - `sync_index_quotes()`：沪深300/中证500/中证1000/上证指数日线（新浪源）→ 新表 `index_quotes(index_code, trade_date, open, high, low, close, volume)`
   - `sync_index_constituents()`：三大指数当前成分股快照 → 新表 `index_constituents(index_code, snapshot_date, symbol, weight)`
2. **重量批次一**（预计 3-5 小时，逐股票，`ingestion/corporate_actions.py`）
   - `sync_share_changes()`：股本变动历史（含公告日期，point-in-time）→ 新表 `share_changes`
   - `sync_dividends()`：分红配股历史（含除权日）→ 新表 `dividends`
   - 两者合并为同一个逐股票循环（每只股票两次请求 + 一次礼貌性sleep），避免翻倍循环开销
3. **重量批次二**（预计 3-5 小时，逐股票，`ingestion/income_statement_batch.py`）
   - `sync_income_statement()`：利润表绝对值（营业总收入/营业利润/净利润等）→ 新表 `income_statement`（EAV设计，与 `fundamentals` 一致）

**衍生计算**（不新增抓取，Phase 1 因子层实现）：
- `market_cap = 最近一次生效的 total_shares × daily_quotes.close`（需按 `announce_date <= 截面日` 对齐，与 disclosure_calendar 同样的 point-in-time 纪律）
- `circulating_market_cap` 同理用 `circulating_shares`
- PS 估值 = `market_cap / income_statement.营业总收入(TTM)`

## 4. 明确不在本轮做 / 记录为已知缺口（2026-08-25更新）

- ~~未复权价格全历史重新抓取（项S）~~：**已关闭**，见第7节
- ~~历史指数成分股变更（项R）~~：**已关闭**（月度point-in-time快照），见第7节
- ~~停牌/复牌精确区间（项T）~~：**已关闭**，见第7节
- 龙虎榜/资金流/融资融券/股权质押/股东人数/大宗交易（项U）：**数据源已验证并提前抓取**（第7节），但行为金融代理信号的**计算与建模仍维持在 Phase 2 开工**，不因数据已到位而提前改变阶段划分
- 北向资金（沪深港通资金流向）：Tushare 2000积分档暂未验证（`moneyflow_hsgt` 等接口可能需要更高档位或单独权限），维持记录为待验证，非阻塞项
- 宏观经济数据（利率/PMI/社融）：维持 Phase 4，不提前

## 5. 自动化执行编排

`scripts/auto_chain_runner.py`：轮询财务指标批量任务是否已结束（通过检测其进程PID是否还存活），
任务结束后按顺序自动依次执行：
`reference_data`（Phase 0.5）→ `market_data`（轻量）→ `corporate_actions`（重量一）→
`income_statement_batch`（重量二）→ `tushare_prices`（复权因子+退市股行情）→
`tushare_market_data`（市值+指数成分+停复牌+涨跌停）→ `tushare_behavior`（Phase2数据预取），
每步写入独立日志文件，全程无需人工干预。已在后台启动，可通过 `logs/auto_chain_runner.log` 追踪整体进度。

## 6. 验收标准

- `trade_calendar` 行数与新浪财经官方交易日历一致（抽样核对最近一个月）
- `index_quotes` 覆盖4个基准指数，最新交易日数据与当前行情一致
- `share_changes` 覆盖率 ≥ 95% 的非退市股票，且每只股票至少有一条 `total_shares` 非空记录
- `dividends` 覆盖率 ≥ 90% 的非退市股票（部分股票上市以来从未分红，属正常0记录）
- `income_statement` 与 `fundamentals` 的 symbol × report_date 交叉覆盖率 ≥ 90%
- `adj_factor` 覆盖率 ≥ 95% 的全部股票（含退市）
- `daily_basic` 覆盖率 ≥ 95% 的非退市股票，且市值与 `share_changes×收盘价` 手算结果误差 < 5%（交叉校验）
- `index_weight` 每个月末交易日3个指数合计成分数应稳定在 300/500/1000 左右（抽样核对）
- `suspend_calendar`/`limit_price` 抽样核对已知停牌事件（如重大资产重组）与官方公告一致
- 全部完成后更新 README「已知限制」章节，明确写出仍然存在的缺口（北向资金/宏观数据），不夸大覆盖范围

## 7. 2026-08-25 追加：Tushare Pro 2000积分档升级后的缺口关闭

用户批准将 Tushare 账户从免费档升级到 2000 积分档（200元/年），升级后限频从"部分接口
1次/小时"提升到"200次/分钟、100000次/天/接口"，此前两轮评估文档记录的多个"技术可行但
被免费档限频/权限挡住"的缺口全部实测打通：

| 接口 | 关闭的缺口 | 新表 | 批处理方式 |
|------|-----------|------|-----------|
| `adj_factor` | 项S（复权因子，用于换算未复权/前复权价格） | `adj_factor` | 逐股票（含退市股），约5500次请求 |
| `daily`（对退市股） | 项B残留（退市股历史行情，此前akshare/sina/eastmoney全部失败） | `daily_quotes`（`adjust='raw'`） | 逐股票，仅约300余只已退市股 |
| `daily_basic` | 项O增强（官方每日市值/PE/PB/PS时间序列） | `daily_basic` | 逐股票，约5500次请求 |
| `index_weight` | 项R（指数历史成分，point-in-time月度快照） | `index_weight` | 3指数×约240个月末交易日 |
| `suspend_d` | 项T（停复牌历史） | `suspend_calendar` | 逐交易日，约2700次请求，全市场快照 |
| `stk_limit` | 额外收获：官方每日涨跌停价格（可与`research/a_share_rules.py`自算规则交叉校验） | `limit_price` | 逐交易日 |
| `top_list`/`block_trade` | 项U（龙虎榜/大宗交易，用户明确提到的"大额买入小额卖出"场景原料） | `dragon_tiger_list`/`block_trade` | 逐交易日 |
| `margin` | 项U（融资融券余额，杠杆/风险偏好代理指标） | `margin_balance` | 逐交易日（按交易所） |
| `moneyflow` | 项U（个股资金流向，大中小单分类，羊群效应代理指标） | `moneyflow` | 逐股票 |
| `pledge_stat` | 项U（股权质押统计，治理风险代理指标） | `pledge_stat` | 逐股票 |
| `stk_holdernumber` | 项U（股东人数，处置效应代理指标） | `holder_number` | 逐股票 |

代码实现：`common/tushare_client.py`（共用 `pro_api()` 客户端 + 逐股票/逐交易日两种通用批处理
编排函数）+ `ingestion/tushare_prices.py` + `ingestion/tushare_market_data.py` +
`ingestion/tushare_behavior.py`。全部在隔离测试库用真实API请求验证通过（非mock），已接入
`scripts/auto_chain_runner.py` 自动执行链的末尾三步。

**明确边界**：`top_list`/`block_trade`/`margin`/`moneyflow`/`pledge_stat`/`holder_number` 六项
属于 Phase 2（行为金融代理信号层）的原始数据，本轮只是提前抓取落地，**不代表 Phase 2 建模工作
提前开工**——项目阶段划分（Phase 0→1→2→3→4）保持不变，仍按"先跑通 Phase 1 量价+财务因子基线
再做 Phase 2 行为金融"的既定顺序推进。
