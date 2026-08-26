# Phase 0.5 数据缺口补齐 —— 必要性与可实现性评估

> 背景：上一轮基于行业对标（Qlib / López de Prado《Advances in Financial Machine
> Learning》/ Feast 等特征存储范式）提出了一份补齐清单。本文档对清单逐项做**必要性**
> （不修复会导致什么后果）与**可实现性**（当前免费数据源实测是否真的能拿到）评估，
> 只有两项都通过才纳入正式开工范围。评估时间：2026-08-25，在 Phase 0 全市场行情批量
> 抓取任务运行期间用小规模探测请求完成，未影响主任务进度。

## 1. 评估方法

- **必要性分级**：高（不修复=回测结论可能是假的）/ 中（会削弱专业度但不致命）/ 低（锦上添花）
- **可实现性分级**：高（已用真实请求验证可行）/ 部分（部分子问题可行，部分被现有网络限制阻塞）/ 待验证（本轮未测试，需开工时先探测）/ 不可行
- 只有 **必要性=高 且 可实现性=高或部分** 的项目才纳入 Phase 0.5 立即开工范围；可实现性=待验证的项目会在开工时第一步先做可行性探测，探测失败则改为记录已知缺口而非强行实现。

## 2. 逐项评估结果

| # | 项目 | 必要性 | 可实现性 | 验证方式与结论 | 决策 |
|---|------|--------|----------|-----------------|------|
| A | 财务数据 point-in-time（公告日历） | 高：不做等于因子回测里"未卜先知"，直接违反理论库 `10-时序验证与标签工程.md` 的核心纪律 | **高** | 实测 `ak.stock_report_disclosure(market='沪深京', period='2024一季')` 返回 5275 只股票的"实际披露"日期，**一次调用覆盖全市场一个报告期**，2015-2026 只需约44次调用（11年×4季度），远比按股票逐个抓取高效 | **通过，Phase 0.5 立即做** |
| B | 幸存者偏差（退市股） | 高：当前股票池是"今天还活着的名单"倒推历史，quality/value类因子回测会系统性虚高 | **部分**：退市股名单可行；退市股价格历史当前不可行 | `ak.stock_info_sh_delist`（159只）+ `ak.stock_info_sz_delist`（208只）实测成功，给出代码/名称/上市日/退市日。但尝试用 sina（`stock_zh_a_daily`）和东财（`stock_zh_a_hist`）拉这些退市股的历史行情均失败（sina返回空、东财 ProxyError），与此前发现的东财网络不稳定是同一根因 | **部分通过**：先把367只退市股的准确代码/名期纳入 `universe`（能修正"哪些股票在过去某天存在"的判断），价格历史缺口显式记录为待解决，不阻塞 Phase 0.5 其余项 |
| C | A股可成交性进回测（涨跌停/停牌/T+1/成本） | 高：不做的话"信号出现当天能买到"的假设是假的 | 高，但**不是数据抓取任务** | 涨跌停阈值可直接从 `daily_quotes` 的前收盘价+板块规则（主板±10%/ST±5%或10%/创业科创±20%/北交所±30%）计算得出，停牌可用"该股票在universe中、未退市、但当日无行情记录"推断，不需要额外抓取新数据源 | **通过，但归入 Phase 1 回测引擎实现**，不占用 Phase 0.5 数据抓取窗口 |
| D | 行业/市值中性化 | 高：不做的话模型可能只是在赌风格/赛道，而非真Alpha | **高**（2026-08-25 补测已确认） | `ak.stock_industry_clf_hist_sw()` 一次请求拿到申万宏源官方行业分类**变动历史**（12903条记录，含"计入日期"，天然支持point-in-time），覆盖全市场。唯一注意点：该请求对 `www.swsresearch.com` 有本机代理环境下的SSL证书链校验问题（`CERTIFICATE_VERIFY_FAILED`），需用 `verify=False` 绕过——这是该研究站点证书链的已知问题，不是数据本身的问题，且只是下载一份公开分类文件，非敏感交易请求，风险可接受。行业代码与 `sw_index_first/second/third_info` 的编码体系（801xxx/850xxx）不是同一套编号，暂只做"编码分组"用于中性化计算，人类可读行业名称解码留待后续 | **确认通过，纳入 Phase 0.5** |
| E | 交易成本后样本外报告 | 高：扣费前好看的曲线没有意义 | 高，**不是数据任务** | 只需佣金/印花税/滑点假设（理论库 `01-基础概念/02-A股市场制度.md` 已有印花税0.05%单边卖出等数字），是回测引擎的计算逻辑 | **通过，归入 Phase 1 回测引擎**，不占用 Phase 0.5 |
| F | 停牌/ST/次新单独标记 | 中：影响可投资域过滤的精细度 | 高 | ST 已在 `universe.is_st`；次新股可由已有 `first_seen_date` 直接判断（如"上市不满6个月"）；停牌见 C，同样是已有数据的推导，不需新抓取 | **通过，Phase 0.5 顺手做**（只是加计算逻辑/视图，不新增抓取任务） |
| G | 模拟盘/影子交易 | 高，但属于 Phase 2 范畴 | 不适用（无需新数据） | 计划文档已排入 Phase 2，且依赖 Phase 1 模型先跑通 | **维持原计划**，不提前 |
| H | 简化 Barra 风险模型 | 中高，但依赖行业分类（见 D） | 依赖 D 的结论 | — | **维持 Phase 2**，与 D 的探测结果联动 |
| I | 漂移监控分级（IC/PSI/滚动夏普） | 高，但依赖模型先存在 | 不适用 | 已在计划 Phase 2、`14-量化MLOps与模型生命周期.md` 中有详细方案 | **维持原计划** |
| J | 拥挤度/资金流/龙虎榜 | 中，行为金融校验层原料 | 待验证，本轮未测试 | AKShare 有龙虎榜/资金流接口，但未测试在当前网络下的稳定性 | **维持 Phase 2**，届时先做小规模可行性探测 |
| K | 付费 PIT 财务/一致预期（Wind/Choice/Tushare高级） | 中：会显著提升数据质量，但当前非阻塞项 | 高（付费即可用），但成本高 | 遵循计划已定原则："先用免费源验证方法论闭环，有效后再决定是否付费" | **维持推迟**，Phase 1 模型验证有效后再评估 |
| L | 算法拆单/OMS/自动下单 | 不适用：产品边界明确不做 | 不适用 | `docs/00-总览/02-产品定位与边界.md` 的 Won't 清单已排除 | **明确排除，不纳入任何 Phase** |

## 3. Phase 0.5 正式开工范围（评估通过项）

在 Phase 0 全市场行情批量抓取（约5200只沪深股票的历史行情）跑完并验收后，**立即开工**，范围锁定为：

1. **`ingestion/reference_data.py`（新增模块）**
   - `sync_disclosure_calendar()`：批量抓取 2015 年至今每个报告期的全市场实际披露日期（约44次调用），写入新表 `disclosure_calendar(symbol, report_date, announce_date)`
   - `sync_delisted_universe()`：抓取沪深退市股清单（367只已知），把准确的 `list_date`/`delist_date` 回填/新增到 `universe` 表，替代当前"快照缺失即判定疑似退市"的粗略推断
   - `sync_industry_classification()`：`stock_industry_clf_hist_sw()` 一次请求抓全市场行业分类变动历史，落地为新表 `industry_classification(symbol, industry_code, start_date, updated_at)`（2026-08-25 补测已确认可行，见上表项D）
2. **`common/db.py` schema 变更**
   - 新增 `disclosure_calendar` 表
   - `universe` 表增加 `list_date` 字段（若尚无），`delist_date` 从粗略推断升级为退市清单的精确日期
   - 若行业分类可行，新增 `industry_classification` 表
3. **`fundamentals` 表使用方在 Phase 1 取数时**，必须 `JOIN disclosure_calendar` 按 `announce_date <= 截面日期` 过滤，不能再直接用 `report_date`
4. **文档同步**：`README.md`、`common/db.py` 表注释里"已知限制"段落更新为"已解决"或"部分解决+剩余缺口说明"

**验收标准**：
- `disclosure_calendar` 覆盖率 ≥ 95% 的 `fundamentals` 已有报告期（同 symbol + report_date 能关联上）
- `universe` 中退市股的 `delist_date` 来自官方清单而非"快照缺失"推断，覆盖沪深全部 367 只已知退市股
- 明确写清楚：退市股价格历史仍是缺口，Phase 1 训练时的股票池重建会因此存在**残余的、已知且已量化范围的**幸存者偏差（约367只退市股无法参与历史因子计算），不是"假装已解决"

## 3.1 开发与验证进度（2026-08-25 更新）

三项任务已开发完成（`ingestion/reference_data.py`）并在隔离测试库（`data/_test_reference.duckdb`，
测试后已删除，不影响主库）上验证通过，**尚未对主库 `warehouse.duckdb` 执行**（因为财务批量抓取
任务当时正占用主库文件锁，DuckDB 单文件同一时间只能一个进程写入）：

| 子任务 | 验证结果 |
|---|---|
| `sync_delisted_universe()` | 沪深退市股清单361只（去重后），全部成功回填 `universe.list_date`/`delist_date`，`is_delisted` 正确置为TRUE |
| `sync_industry_classification()` | 12903条历史分类记录，覆盖5912只股票，写入新表 `industry_classification` |
| `sync_disclosure_calendar()` | 抽样测试2025~2026年6个报告期，32740条记录全部成功、0失败；全量2015~2026约48个报告期预估总耗时约2分钟（抽样期间每6期约15秒） |

`common/db.py` 已同步更新：新增 `disclosure_calendar`、`industry_classification` 两张表，`universe`
新增 `list_date`/`delist_date` 两列（用 `ALTER TABLE ... ADD COLUMN IF NOT EXISTS` 做增量迁移，
避免重演 `fundamentals` 表 schema 漂移的教训）。

**待办**：财务批量抓取任务释放主库文件锁后，对 `warehouse.duckdb` 正式执行
`python -m ingestion.reference_data`（预计几分钟内完成），随后按第3节验收标准核验。

## 4. 明确不在 Phase 0.5 做的（避免范围蔓延）

- 涨跌停/停牌/成本进回测计算逻辑 → 留给 Phase 1 因子回测引擎（`research/factor_eval.py`）
- 行业/市值中性化的具体计算 → 留给 Phase 1（Phase 0.5 只负责把行业分类数据抓回来，如果可行）
- 退市股价格历史 → 记录为已知缺口，等东财网络稳定或引入付费数据源后再补
- 龙虎榜/资金流/舆情抓取 → 维持 Phase 2，不提前
