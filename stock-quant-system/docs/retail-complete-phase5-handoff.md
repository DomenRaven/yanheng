# Phase 5 对接文档 —— 完整散户决策链（模拟盘 → 年底小资金实操）

> **读者**：在新窗口开工的 Agent / 开发者。  
> **用户目标（2026-09-16 确认）**：做完整的「方向 1」——选股、择时、仓位、买卖建议全覆盖；**先模拟盘**，**年底约 1 万元**小资金实操（用户自行在券商下单，系统不代客报单）。  
> **明确不做**：秒级量化、高频、无资质代客下单、收益承诺（见 `docs/00-总览/02-产品定位与边界.md` Won’t）。

---

## 1. 一句话要交付什么

把现有「日频排序 + 规则建议」补成 **可闭环的散户工作流**：

```text
数据更新 → (可选)重训 → scanner Top-N
    → 建仓计划（权重 + 股数 + 金额）
    → 择时剧本（下一交易日怎么买/什么条件作废）
    → 模拟盘成交与净值
    → 持仓日更建议（买/卖/减/止盈止损，带股数）
    → 复盘（建议 vs 模拟/用户确认实盘）
```

**验收口径**：用户能在本机用 **模拟资金** 跑通上述链路至少 4 周；年底 1 万实操时只需切换「实操确认」模式，不改核心规则。

---

## 2. 必须先读的仓库约定

| 文档 / 规则 | 用途 |
|-------------|------|
| `.cursor/rules/quant-dev-loop.mdc` | 8 闸门：对齐需求 → 实现 → 测试打分 → 验收报告 |
| `.cursor/rules/vibe-coding-ethics.mdc` | 八荣八耻；A 股规则查 `research/a_share_rules.py` |
| `.cursor/skills/yanheng-dev-loop/SKILL.md` | 本项目绑定的八闸门 Skill |
| `docs/00-总览/02-产品定位与边界.md` | Must/Should/Won’t |
| `docs/07-产品设计启示/02-场景化建议引擎.md` | 建议卡片 Schema（`size_pct_nav`、`invalid_if` 等） |
| `docs/HANDOFF-NOTES.md` | 环境坑、DuckDB 单写者、并行 ingestion |
| `docs/9.16-散户决策链需求规格.md` | **2026-09-16 需求正文**：散户 JTBD、Must/Should、与代码逐项对照、相对本文的微调 |
| `docs/9.16-项目状态报告.md` | 同日项目状态（含数据更新只读监测） |
| `docs/phase5-development.md` | 阶段映射、表、模块、测试、ADR |
| `docs/phase5-constraints.md` | 分层/A股/Won’t/灌库/测试硬约束 |
| `../../docs/00-总览/08-第四轮散户决策链补齐对照.md` | 理论缺口、文献下载记录、交叉验证 |
| `docs/phase0.5-gap-closing-assessment.md` 项 G | 模拟盘原计划在 Phase 2 范畴，**至今未实现** |

数据更新（并行进行中的全量灌库）见 `.cursor/skills/stock-quant-data-update/SKILL.md`；**不要**为查进度杀写库进程。

---

## 3. 现状 vs 缺口（对照用户清单）

| 能力 | 现状（代码） | Phase 5 要补 |
|------|----------------|---------------|
| 选潜力股 | `advice/scanner.py` + 冠军 `mlops/registry/champion.json` | Top-N **理由包**；可选行业暴露/轮动过滤（`industry_classification`） |
| 入手时机 | 日频排名 + 涨跌停过滤 | `advice/entry_rules.py`（可回测）：信号日/执行日分离 + **明日操作清单** |
| 持仓数量 | `risk/portfolio_optimizer.py` → 目标权重 | 对接账户净值 → **100 股整数股数 + 金额 + 占净值%** |
| 买入时机 | `open` 仅「前 50 名可考虑」 | `open`/`add` 带 `size_shares`、`price_band`、`invalid_if` |
| 卖出时机 | `advice/advice_engine.py`：±8%、集中度 | 排名失效、持有期满、再平衡 → **建议卖出股数** |
| 全流程 | 手动 `positions` 录入 | **模拟盘**表 + UI；实操 = 用户确认成交回写 |
| 消息面 | `llm/explain_assistant.py` 解释已给数据 | 可选公告摘要（有源引用）；**不**做全自动消息面交易 |

---

## 4. 关键代码地图（扩展时优先复用）

```text
advice/
  scanner.py              # 全市场打分 → prediction_log
  advice_engine.py        # 建议卡片；扩展 size_shares / invalid_if
  (新) entry_rules.py     # 择时纯函数，供引擎与回测共用

risk/
  portfolio_risk.py       # 集中度、VaR；load 手动 positions
  portfolio_optimizer.py  # Grinold-Kahn alpha + MV/风险平价 → 目标权重

research/
  panel.py                # 唯一摸库特征面板
  train_lightgbm.py       # 三重障碍 ±8% / 20 日；LambdaRank
  labeling.py             # triple_barrier 数学定义
  validation.py           # Purged K-Fold / Walk-Forward

mlops/
  retrain_schedule.py     # 周末重训 + 冠军晋升（Walk-Forward RankIC）

common/db.py              # positions, advice_log, prediction_log；模拟盘表在此扩展

pages/
  1_持仓与建议.py
  7_历史建议复盘.py       # 复盘需升级时挂 paper/实盘对比

scripts/
  run_data_update.py      # 数据更新入口
  watch_data_update.py    # 只读进度监测
```

**生产冠军（截至交接时）**：`champion.json` → `run_id: 20260826_123439`（LightGBM）。scanner **只读 champion**，不读「最新文件夹」。

---

## 5. 分阶段实施（建议开工顺序）

### 阶段 A — 模拟盘内核（阻塞项，先做）

**目标**：与本机 DuckDB 一体的 **纸面账户**，与现有 `positions`（手动实盘录入）可并存或模式切换。

**建议表结构（草案，实现时在 `common/db.py` + `init_schema` 落地）**：

| 表 | 用途 |
|----|------|
| `paper_account` | `account_id`, `initial_cash`, `created_at`, `note`（如「默认 10 万练习 / 1 万实操预设」） |
| `paper_cash` | 当前现金（或每次从 trades 滚动计算，二选一，文档写清） |
| `paper_positions` | `symbol`, `shares`, `avg_cost`, `opened_at`（可合并 lot 或保留 lot） |
| `paper_trades` | `trade_id`, `symbol`, `side`, `shares`, `price`, `fees`, `trade_date`, `source`（sim / user_confirmed_live） |
| `paper_nav_daily` | 可选：每日总市值 + 现金快照，供曲线 |

**成交规则（必须写进代码与验收）**：

- A 股 **100 股整数**；**T+1**（当日买入不可当日卖）。
- **涨跌停 / 停牌**：买不进、卖不出（查 `limit_price`、`suspend_calendar` + 当日行情）。
- **费用模板**：佣金（双边）、印花税（卖侧）；配置放 `config.yaml` 的 `paper_trading` 段。
- **滑点**：默认简化（如开盘价 ± 固定 bp），参数可配置；**如实标注**非真实盘口。

**入口**：

- 库函数：`paper/broker_sim.py` 或 `advice/paper_broker.py`（择名与 `ingestion` 区分）。
- CLI 或 Streamlit：「按今日建仓计划模拟成交」。

**验收**：

- 从 scanner 输出 3 只股票 → optimizer 权重 → 生成买单 → 模拟成交 → `paper_nav` 变化正确。
- 重复跑 **幂等** 或明确「仅执行新建议」语义，避免双倍下单。

---

### 阶段 B — 可执行建议（与 A 强耦合）

**目标**：`advice_log` 与 UI 卡片对齐 `docs/07-产品设计启示/02-场景化建议引擎.md`。

**字段补齐**（部分列已存在 JSON 列，可渐进）：

- `size_pct_nav` / `size_shares` / `est_amount_cny`
- `horizon_days`
- `confidence`（初版：排名分位或固定映射；勿伪造统计显著性）
- `invalid_if_json`（已有列，要 **生成内容**）
- `constraints_applied`：T+1、单票上限 15%（与 `_SINGLE_NAME_LIMIT` 一致）

**修改焦点**：`advice_engine.py` 调用 optimizer 时传入 **paper 净值** 或用户选定账户；`open` 文案去掉「仓位自己把控」，改为具体股数。

**验收**：

- 随机抽 5 张卡片，能回答：买/卖多少股、约占净值多少、什么情况下建议作废。
- 1 万预设：买不起 1 手时必须 `watch` + 原因，不能 `open`。

---

### 阶段 C — 择时与「明日操作清单」

**目标**：日频 **执行剧本**，不做到秒级。

**新模块** `advice/entry_rules.py`（纯函数）示例规则（实现前用文档条款对齐用户确认）：

1. **信号日**：收盘后 scanner/排名确认。  
2. **执行日**：下一交易日；若开盘涨停则不买；否则按 **开盘价** 或 **收盘价** 成交假设（二选一，见 §6 待决）。  
3. **失效**：信号日后跌破 N 日均线、排名跌出 K、放量破位等 → `invalid_if`。

**回测**：复用 `research/validation` 时间轴；单独脚本 `scripts/backtest_entry_rules.py` 或挂现有回测框架；报告写进 `docs/phase5-acceptance-report.md`（新建）。

**UI**：新页或扩 `1_持仓与建议.py` — **「明日待办」**（最多 1–3 只，适配小资金）。

**验收**：

- Walk-forward 或滚动窗口给出：成交率、相对「信号日收盘立刻买」的成本差（**写局限**）。

---

### 阶段 D — 复盘与年底实操模式（10–12 月）

- 模拟盘周报：收益、回撤、换手、最大单票暴露、绑定的 `model_run_id`。
- **实操模式**：用户在券商成交后点「确认」→ 写入 `paper_trades.source=user_confirmed_live` 或独立 `live_trades`（设计时二选一）。
- 与 `7_历史建议复盘.py` 对齐：建议价 vs 成交价偏差。

**验收**：

- 至少一条完整 trace：advice_id → 建议 → 模拟/确认成交 → 复盘统计。

---

## 6. 开工前待用户拍板（未决则用默认）

| 议题 | 选项 | **建议默认**（无回复时按此实现） |
|------|------|----------------------------------|
| 模拟初始资金 | 10 万练习 / 1 万贴近年底 | **双预设**：`paper_account` 模板 `practice_100k` + `live_prep_10k` |
| 买入成交假设 | 次日开盘 / 次日收盘近似 | **次日开盘价**（涨跌停不可买）；回测备注滑点 |
| 与 `positions` 关系 | 合并 / 分离 | **分离**：模拟用 `paper_*`；原 `positions` 保留给「已录入实盘」 |
| 自动券商下单 | 做 / 不做 | **不做**（产品 Won’t）；仅导出待办 + 模拟 |

---

## 7. 用户侧日常流程（目标态）

1. **收盘后**：`run_data_update`（或确认已完成）→ `mlops.retrain_schedule`（周末）→ `advice.scanner`  
2. 打开 **明日待办** + **建仓/调仓计划**（股数）  
3. **次日**：券商手动下单 **或** 点模拟成交  
4. **收盘后**：持仓建议卡片（卖/减/止损）  
5. **周末**：`drift_monitor` + 模拟盘周报  

**1 万元约束（产品预设）**：最大同时持仓 **2–3** 只、单票 **≤15%** 净值、现金缓冲 **≥10%**（可 `config.yaml` 配置）。

---

## 8. 测试与验收纪律

- 任何「规则有效」结论：**真实 DuckDB 查询或回测数字**，禁止「应该没问题」。  
- 小步提交：先 A（模拟盘）→ B（股数）→ C（择时）→ D（复盘）；**禁止**一次改十个 Phase。  
- 验收报告：新建 `docs/phase5-acceptance-report.md`，逐条对照本文 §5 各阶段验收。  
- 清理：一次性脚本、`__pycache__` 任务结束即删。  

**建议自动化测例**：

- `scripts/test_paper_broker.py`：T+1、100 股、涨停不可买、费用。  
- 扩展现有 `test_concentration.py` 思路测 1 万买不起 1 手。

---

## 9. 与进行中的数据更新关系

- 全量 `run_data_update --fresh-run` 可能仍占用 `warehouse.duckdb`；开发模拟盘 schema 时用 **临时库** 或等写库结束。  
- 模型/扫描依赖 `daily_quotes` 等；数据缺口大时 **先更新再 scanner**，重训非强制但推荐（`retrain_schedule`）。  

---

## 10. 合规与文案（所有新 UI 必须带）

- 辅助研究工具，不构成投资建议；模拟成交不等于实盘结果。  
- 展示 **数据截止时间**、**模型 run_id**、**规则假设**（费率、滑点、成交价口径）。  

---

## 11. 新窗口开工 Checklist

- [x] 读本文 + `02-产品定位与边界.md` + `02-场景化建议引擎.md`（2026-09-16）  
- [x] 读 `docs/9.16-散户决策链需求规格.md`（需求正文 + 实现对照；微调见该文 §8）  
- [x] 读 `docs/phase5-development.md` + `docs/phase5-constraints.md` + 理论 `08-第四轮…`（2026-09-16 文档轮）  
- [ ] 确认 §6 待决项（或用默认；规格已按默认写入）  
- [ ] 阶段 A：schema + `paper_broker` + 最小 UI/CLI  
- [ ] 阶段 B：`advice_engine` 股数与 `advice_log` 字段  
- [ ] 阶段 C：`entry_rules` + 明日待办 + 回测报告  
- [ ] 阶段 D：复盘与实操确认  
- [ ] 写 `phase5-acceptance-report.md`  

---

## 12. 相关历史对话上下文（摘要）

- 用户曾问两条路线；选定 **完整方向 1**，拒绝秒级量化。  
- 当前系统效用感弱的原因：预期是「全自动炒股」，实际是 **日频研究辅助**。Phase 5 目标是 **缩小落差**，仍不自动券商下单。  
- 监测脚本：`python -m scripts.watch_data_update`（日志可能为 UTF-16，已处理）。  

---

*文档版本：2026-09-16，随实现更新 §5 验收与 §6 待决。需求细化与对照以 `docs/9.16-散户决策链需求规格.md` 为准；本文件保留分阶段工程草案。*
