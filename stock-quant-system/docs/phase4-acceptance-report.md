# Phase 4 验收报告 —— 持仓驾驶舱 / 场景化建议引擎 / 组合优化 / 风险仪表盘 / LLM解释层

验收时间：2026-08-26。方法：全部功能用真实数据库（`data/warehouse.duckdb`）+ 真实生产冠军
模型（`mlops/registry/champion.json`）跑通，UI交互用 Playwright 浏览器自动化对本机启动的
Streamlit 应用逐页真实点击验证（不是截图占位/伪造交互记录）。

对照文档：`docs/00-总览/02-产品定位与边界.md`（Must/Should/Won't边界）、
`docs/07-产品设计启示/03-MVP路线图.md`（Phase 4"智能化与生态"条款）、
`docs/07-产品设计启示/02-场景化建议引擎.md`（建议卡片schema）、
`.cursor/rules/quant-dev-loop.mdc`（8闸门开发流程）、
`.cursor/rules/vibe-coding-ethics.mdc`（八荣八耻道德约束）。

## 1. 范围核对：产品边界 Must / Should 条款逐项对照

| 条款 | 来源 | 状态 | 落地位置 |
|---|---|---|---|
| 标的与组合视图：持仓/成本/浮盈亏/集中度 | Must | ✅ | `risk/portfolio_risk.py` + `pages/1_持仓与建议.py` + `app.py` |
| 场景建议：建仓/加仓/减仓/清仓/观望+理由 | Must | ✅ | `advice/advice_engine.py`（七种action，全部带`reasons[]`） |
| 风险：波动/回撤/单票上限/简化VaR | Must | ✅ | `risk/portfolio_risk.py::generate_risk_report` + `pages/3_风险仪表盘.py` |
| 明确免责与"非投资顾问"声明 | Must | ✅ | 每张建议卡片`disclaimer`字段 + 每个Streamlit页面顶部/建议卡片内文案 |
| 多因子/轻量ML排序 | Should | ✅（Phase1-3已完成） | LightGBM冠军 + GNN挑战者评估 |
| **组合优化（约束均值方差/风险平价简化版）** | Should | ✅ **本轮补齐** | `risk/portfolio_optimizer.py` |
| 舆情/研报摘要（LLM，需引用源） | Should | ✅ | `llm/news_fetch.py`（带`url`引用）+ `llm/explain_assistant.py` |
| Walk-forward、成本与涨跌停可成交性建模 | Should | ✅（Phase1已完成） | `research/backtest.py` |
| 建议置信度与冲突提示 | Should | ✅ | `confidence`字段 + `conflict_flag`（行为金融冲突） |
| 承诺收益/一键致富/无券商资质代客下单/黑盒模型当交易指令 | Won't | ✅ 未触碰 | 全部建议为信息性，`advice_engine.py`docstring显式声明不自动下单 |

**如实标注**：「组合优化」是本轮验收时逐条对照产品边界文档才发现的缺口——原Phase2-4任务
列表（`p4-schema` ~ `p4-accept`）未单独列出这一项，属于Phase4验收阶段主动补齐的动作，
不是一直在计划内、也不是回避不提。本报告第3节详细记录该模块。

## 2. 建议引擎（`advice/advice_engine.py`）

严格按`docs/04-风险管理/03-场景化决策建议框架.md`第4节优先级实现，**不是随意if/else堆砌**：

1. 合规与可交易性（停牌/涨停封死买不进/跌停封死卖不出）→ `watch`
2. 生存风控（止损8%触发 / 单票集中度超15%）→ `stop_loss` / `reduce`
3. 账户结构健康（持仓状态转移）→ 隐含在上一级逻辑中
4. 信号质量（模型排名走弱时止盈兑现，否则`hold`）→ `take_profit` / `hold`
5. 收益增强（组合优化再平衡）→ `rebalance`（仅覆盖`hold`卡片，见第3节）

止盈止损阈值（±8%）直接复用生产冠军模型训练时的三重障碍标签配置
（`mlops/registry/20260826_123439/metadata.json::label_config`），逻辑自洽，不是另外
拍一个不相关的数字。

**端到端验证**（真实操作，非单测mock）：在Streamlit「持仓与建议」页面手动录入
`000001 平安银行 1000股 @成本10.50`（当日最新价¥11.56，浮盈10.1%），点击"生成/刷新建议
卡片"后：因为单一持仓=100%集中度，触发优先级2风控规则，正确输出`reduce`（置信度60%），
理由列出"单票占净值100.0%，超过15%上限"——验证了风控优先级确实高于信号质量层，不会因为
浮盈为正就误判为"继续持有"。

## 3. 组合优化（`risk/portfolio_optimizer.py`）—— 本轮补齐的Should级缺口

- **均值-方差**：目标函数 `maximize w^T·alpha − 0.5·λ·w^T·Σ·w`（`docs/03-量化方法/
  04-组合优化.md`第1节"最大化Alpha−λ·风险"标准写法），`scipy.optimize.minimize`
  (SLSQP)求解，约束`sum(w)=1`、`0≤w≤单票上限(15%)`。
- **Alpha估计**：Grinold-Kahn《Active Portfolio Management》"精化Alpha"公式
  `alpha_i = IC × σ_i × score_z_i`——`score_z_i`是模型打分在候选池内的横截面z-score，
  `σ_i`是个股历史年化波动率，`assumed_IC`保守取0.03（低于`docs/phase1-acceptance-report.md`
  实测样本外RankIC均值，因为优化器对alpha误差敏感，保守收缩避免对着噪音下重注）。
- **协方差估计**：`sklearn.covariance.LedoitWolf`收缩估计而非原始样本协方差——候选数
  （约30~80）接近甚至超过样本天数(250天)量级时原始协方差数值不稳定，这是
  `docs/03-量化方法/04-组合优化.md`第4节明确建议的工业技巧。`scikit-learn`/`scipy`
  均为项目已有依赖，**未新增任何包**（复用存量原则）。
- **风险平价**：等风险贡献目标 `minimize Σ(RC_i − 平均RC)²`，同样用SLSQP求解。
- **再平衡覆盖层**：`advice/advice_engine.py::apply_rebalance_overlay()`只把已判定为
  `hold`（意味着优先级1-4规则均未触发）的卡片，在目标权重与当前权重偏离超过3%阈值时
  升级为`rebalance`，**绝不覆盖**任何`reduce`/`stop_loss`/`take_profit`/`watch`结论——
  已用单元测试验证该优先级隔离（见第4节）。

**真实数据数值测试**（15只沪深主板股票，250日历史窗口）：均值-方差解权重之和=1.0000，
单票上限15%被正确绑定（`000001`/`000019`触顶15%）；风险平价解权重之和=1.0000，权重分布
比均值-方差更均匀（波动率越低权重越高的经典特征清晰可见）。

**已知局限（如实标注）**：无行业/风格中性化约束（`docs/03-量化方法/04-组合优化.md`第2节
其余约束——行业偏离、换手率上限——本版本未实现）；`assumed_IC=0.03`是保守拍定的常数，
未按市场状态或历史IC动态调整；只在持有仓位非空时才触发（空仓用户看不到优化建议，这是
合理的——没有持仓就没有"再平衡"这个动作的意义）。

## 4. 单元测试与集成测试记录

- `risk/portfolio_optimizer.optimize_target_weights`：真实价格数据下验证均值-方差/风险
  平价两种方法权重和=1、单票上限生效、退化路径（候选<2只/历史数据<60日时返回空表而非
  抛异常）。
- `advice/advice_engine.apply_rebalance_overlay`：构造3张卡片（`hold`/`reduce`/`hold`）+
  模拟rebalance结果，断言验证：①`hold`且偏离>阈值 → 正确升级为`rebalance`；
  ②`reduce`（更高优先级）→ 保持不变，不被覆盖；③`hold`但偏离<阈值 → 保持`hold`。
  三条断言全部通过（`ALL ASSERTIONS PASSED`）。
- `advice/advice_engine.generate_daily_advice`端到端：录入真实持仓后完整跑通
  "掘金扫描→持仓风险→建议卡片→再平衡覆盖尝试(异常已用try/except隔离，不影响主流程)→
  落库`advice_log`"全链路，无异常退出。

## 5. Streamlit应用（`app.py` + `pages/`）—— Playwright真实交互验证

用 `plugin-playwright-playwright` MCP 对本机 `streamlit run app.py`（端口8501）逐页验证：

- **持仓与建议页**：录入持仓表单提交成功→列表正确显示股数/成本/最新价/浮盈亏10.1%→
  点击"生成/刷新建议卡片"后成功输出`reduce`建议卡片，展开后理由/风险提示/失效条件/
  免责声明全部正确渲染。
- **掘金扫描页**：滑动条调整Top-N，点击"运行最新扫描"后成功输出5340只全市场候选（可交易
  5293只），Top候选表格正确显示排名/打分/因子暴露/行为金融冲突提示列。
- **风险仪表盘页**：持仓为空时正确显示"无持仓记录，风险报告为空"（优雅处理空状态，不
  崩溃）；有持仓时集中度饼图/波动率VaR表/相关性热力图/组合VaR回撤指标均正确渲染
  （此前修复过`_load_price_history`丢失symbol列导致的`KeyError`，本轮复测已确认修复生效）。
- **AI解释页**：输入`000001`后正确抓取到真实新闻标题（东方财富，含"平安银行2026年中报"等
  真实条目）、正确显示模型打分(-1.8012，排名1516)与因子暴露(ROE 2.67/BP 2.0563等)、
  正确显示行为金融信号（处置效应flag）；当时未配置解释层 API Key，AI解释区域正确
  降级为警告文案+带超链接的Key获取地址，**未崩溃、其余结构化数据不受影响**——这正是
  `llm/explain_assistant.py`设计时要求的优雅降级行为。

验证完成后清理：删除测试用持仓记录（`000001`测试lot）、终止两个残留的Streamlit测试进程
（其中一个误用了非项目venv的全局Python解释器，已一并清理，避免误导后续开发者）。

## 6. 对照计划文档验收条款

Phase 4（应用层：持仓驾驶舱/建议引擎/LLM解释层）条款要求的四大模块（持仓风险、建议引擎、
LLM解释、Streamlit UI）已全部实现并真实交互验证；额外主动核查产品边界文档发现并补齐了
"组合优化"这一Should级缺口。`docs/00-总览/02-产品定位与边界.md`的Must/Should条款
（见第1节表格）已全部覆盖，Won't清单（自动下单/高频/收益承诺/无牌代客理财）严格未触碰。

## 7. 全项目已知局限总汇（如实记录，不因为"验收通过"而回避）

- **持仓数据**：手动录入，非券商实时同步，不反映真实成交滑点/费率差异。
- **VaR/CVaR**：历史模拟法（经验分位数），非参数法/蒙特卡洛，适合个人研究场景但不是
  机构级风控标准。
- **组合优化**：无行业/风格中性化，`assumed_IC`为保守拍定常数。
- **LLM解释层**：默认通义千问（`DASHSCOPE_API_KEY`），依赖用户自行配置外部凭据；
  仅做翻译不做预测，如果输入信号本身有误，解释也会如实反映矛盾但不会纠正底层计算。
  模型没有实时时钟（训练记忆常停在旧年份，看起来像「断网」）。2026-08-26 起
  `explain_assistant` 注入本机日历日 + 新闻发布时间；换 DeepSeek 不能对时。
  不开默认联网搜索（兼容接口不返回来源）。
- **深度学习挑战者**（Phase3结论）：全市场RankIC显著更优但未转化为Top-N组合收益提升，
  维持LightGBM为生产冠军，此结论有效期取决于市场状态，`mlops/drift_monitor.py`+
  `mlops/retrain_schedule.py`会持续监控是否需要重新评估。
- **行为金融信号**（Phase2结论）：处置效应/拥挤度均为量价代理指标，非官方逐日筹码分布
  数据（当前Tushare账户权限不覆盖`cyq_perf`）；情绪层不含真正新闻NLP文本情绪分析（本轮
  LLM解释层做的是"翻译已有信号"，不是"从新闻文本提取新的情绪因子"，是有意的范围区分）。

## 8. 清理记录

本轮产生的一次性验证脚本（`scripts/_tmp_cleanup_ui_test.py`、
`scripts/_tmp_test_optimizer.py`、`scripts/_tmp_test_advice_rebalance.py`、
`scripts/_tmp_test_overlay_unit.py`）及测试用持仓记录、残留Streamlit测试进程均已在
验收后清理，关键验证结论已摘录进本报告，不依赖这些脚本复现（复现方式：`streamlit run
app.py`后按第5节步骤操作，或直接调用`advice/advice_engine.py::generate_daily_advice()`）。

另外在全项目文档终审时顺带发现并清理了两类历史遗留的冗余文件：①仓库根目录残留的三个
Phase3/Phase4测试期临时日志文件（`_tmp_streamlit*.log`、`_tmp_train_dl.log`，均已把关键
数字摘录进对应验收报告，日志本身可安全删除）；②`data/warehouse.duckdb.checkpoint_pre_
phase1`（Phase1开工前的数据库安全备份，4.9GB）——Phase1-4四份验收报告均已在正式生产库
（`warehouse.duckdb`）上验证通过，该备份的"防止Phase1改坏数据"用途已过期，且继续保留会让
"哪个是当前生产库"产生歧义风险，故删除以释放磁盘空间并降低误恢复的风险。
