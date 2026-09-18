# Phase 5 验收报告 —— 散户决策链（模拟盘 → 可执行建议）

> **对照**：`docs/9.16-散户决策链需求规格.md` §5 Must  
> **约束**：`docs/phase5-constraints.md`  
> **开发**：`docs/phase5-development.md`

## 1. 阶段 A — 模拟盘内核（2026-09-17）

### 1.1 交付物

| 路径 | 说明 |
|------|------|
| `common/db.py` | `paper_account` / `paper_cash` / `paper_positions` / `paper_trades` |
| `config.yaml` | `paper_trading` 模板与滑点 |
| `advice/paper_broker.py` | 开户、成交、净值、幂等 |
| `scripts/test_paper_broker.py` | 临时库验收 |
| `pages/1_持仓与建议.py` | 模拟盘入口（与 `positions` 分离） |

### 1.2 验收条款（Must）

| ID | 条款 | 结果 | 证据 |
|----|------|------|------|
| M4 | T+1 当日买不可当日卖 | **通过** | `test_paper_broker.test_lot_size_and_buy_sell_tplus1` |
| M9 | 本机模拟账户/持仓/成交 | **通过** | 四表 + `create_paper_account` / `execute_paper_order` |
| M10 | 费用走 `apply_costs`；滑点可配 | **通过** | `config paper_trading.slippage_bp`；最低佣金 5 元测例 |
| M14 | 与 `positions` 分表 | **通过** | schema 分离；broker 不写 `positions` |
| M19 | 同一 `advice_id` 不双倍成交 | **通过** | `test_idempotent_advice_id` |
| M3 | 建议层与模拟同一可交易规则 | **部分** | 模拟用 `limit_price`+停牌+涨跌停；建议层 `is_tradable` + `entry_rules` 次日开盘涨停；**未**一键 scanner→批量模拟 |

### 1.3 测试命令

```powershell
cd stock-quant-system
$env:STOCK_QUANT_DB = (Resolve-Path data\e2e_test.duckdb).Path  # 可选
.venv\Scripts\python.exe scripts\test_paper_broker.py
```

### 1.4 已知局限（#7）

- 成交价默认 **次日开盘价 ± 配置滑点**，非 Level-2 盘口。  
- DuckDB **不支持** partial unique index；`advice_id` 幂等靠应用层查询，非 DB 约束。  
- 生产库 **DDL**：首次调用 `write_session(init=True)` 或 Streamlit 模拟盘开户时执行 `init_schema`；全量 `run_data_update` 占用写锁时 UI 写入会提示 busy。  
- 阶段 A **未**含：scanner→权重→一键批量模拟（留 UI 深化）。

---

## 2. 阶段 B — 可执行建议（2026-09-17）

### 2.1 交付物

| 路径 | 说明 |
|------|------|
| `advice/position_sizing.py` | 以损定仓 + 100 股取整 |
| `advice/advice_engine.py` | `enrich_cards_with_sizing`；open 文案；`advice_log` 增列读写 |
| `config.yaml` | `risk_fraction` / `max_single_weight` / `min_cash_buffer` / `max_concurrent_positions` |
| `common/ui_theme.py` | 卡片展示股数/金额/占净值/最大亏损 |
| `scripts/test_advice_sizing.py` | 1 万买不起贵股 1 手 |

### 2.2 验收条款

| ID | 条款 | 结果 | 证据 |
|----|------|------|------|
| M5 | `size_shares` / `est_amount_cny` | **通过** | 落库 + UI metric |
| M6 | `size_pct_nav` | **通过** | open 为单值；持仓卡片保留原权重列表语义 |
| M7 | 以损定仓 + 最大亏损 | **通过** | `max_loss_cny`；`risk_fraction=0.01` |
| M8 | 买不起 1 手 → watch | **通过** | `test_advice_sizing` ¥500×100；引擎降级 |
| M12 | `horizon_days` / `exec_date` | **通过** | config 20 日 + `next_trade_date` |
| M13 | open 可执行 `invalid_if` | **通过** | `actionable_invalid_if_open` |
| M17 | 去掉「仓位自己把控」 | **通过** | `_plain_summary` 改写 |
| M18 | 禁止浮亏加仓 | **通过** | 引擎无 `add` 动作（回归约束） |

净值默认：无 paper 账户时用 `default_template_for_advice`（`live_prep_10k`）；有 paper 账户时用最新账户 NAV。

### 2.3 测试命令

```powershell
.venv\Scripts\python.exe scripts\test_advice_sizing.py
```

---

## 3. 阶段 C — 择时与明日待办（2026-09-17）

### 3.1 交付物

| 路径 | 说明 |
|------|------|
| `advice/entry_rules.py` | 执行日、开盘涨停/停牌拦截、待办列表 |
| `pages/8_明日待办.py` | ≤3 条待办 |
| `pages/1_持仓与建议.py` | 生成后摘要待办 |
| `scripts/test_entry_rules.py` | `next_trade_date` + `build_tomorrow_todos` |
| `scripts/backtest_entry_rules.py` | 验收用数字（保留回归） |

### 3.2 验收条款

| ID | 条款 | 结果 | 证据 |
|----|------|------|------|
| M11 | 明日待办 1–3 只 | **通过** | `build_tomorrow_todos(..., max_items=3)` + 页面 |

### 3.3 回测数字（诚实边界，非超额承诺）

在 `warehouse.duckdb` 上，近 120 日、低价股样本、**下一根 K 线作执行日**的简化统计（2026-09-17 跑数）：

| 指标 | 数值 |
|------|------|
| 样本笔数 | 6414 |
| `entry_blocked_at_open` 拦截比例 | **0.0%**（`limit_price` 覆盖/对齐需单独审计；拦截逻辑已实现） |
| 可成交时 开盘相对信号收盘 | 均值 **-0.15%**，中位 **-0.06%** |

**不**解读为「择时增强收益」——见需求规格 §8 与 `docs/03-量化方法/15-日频信号与次日执行.md`。

```powershell
.venv\Scripts\python.exe scripts\backtest_entry_rules.py
.venv\Scripts\python.exe scripts\test_entry_rules.py
```

---

## 4. 阶段 D — 复盘与实操确认（2026-09-17）

### 4.1 交付物

| 路径 | 说明 |
|------|------|
| `pages/7_历史建议复盘.py` | 展示股数字段；`paper_trades` 按 `advice_id` 关联；M15 确认表单 |
| `advice/paper_broker.py` | `latest_paper_account_id`；`source=user_confirmed_live` |

### 4.2 验收条款

| ID | 条款 | 结果 | 证据 |
|----|------|------|------|
| M15 | 用户确认成交回写 | **通过** | `execute_paper_order(..., source='user_confirmed_live', price_override=...)` |
| M16 | 建议→成交→复盘至少一条 `advice_id` | **通过（代码）** | `scripts/test_advice_trace.py`：persist→成交→`load_advice_traces` 完整行 1 条；`fill_vs_advice_close` 相对建议日收盘 |
| S6–S7 | 周报 / 完整 trace | **部分 / S7 已绑 M16** | S6 expander 摘要；S7 偏差表改走 `advice_trace`（非页面重算） |

完整 trace 路径：生成建议 → 明日待办 → 模拟或 M15 确认 → 复盘页 metric「全链路 trace」≥1。生产库 0 条 = 尚未成交，不是 schema 缺口。

---

## 5. 总表状态

| 阶段 | 状态 |
|------|------|
| A 模拟盘内核 | **完成**（M3 部分 → 见 §6） |
| B 可执行建议 | **完成** |
| C 明日待办 | **完成** |
| D 复盘 / 确认 | **Must 完成**；S6 周报 UI 已挂模拟盘 expander |

---

## 6. 闭环补强（2026-09-17 第二轮）

对照上轮「待完成」清单，本轮回补：

| 项 | 交付 | 验收 |
|----|------|------|
| M9/M3 批量模拟 | `simulate_advice_cards`；`pages/1` 按钮「按今日建议批量模拟成交」 | `scripts/test_paper_batch_simulate.py` **通过** |
| M17 合规条 | `ui_theme.render_trust_footer`；`app.py` / 持仓 / 复盘 / 待办 | 页脚含数据截止、model_run_id、滑点说明 |
| M8 持仓 invalid_if | `actionable_invalid_if_position` + enrich | open/持仓类均可核对 |
| M16/S7 价对比 | `pages/7` 单条偏差 + 聚合表 | 建议日收盘 vs 成交价 |
| S6 周报 | `paper_weekly_report` + 模拟盘 expander 四指标 | 近 7 日成交/净值/换手/最大单票 |
| limit_price 审计 | `scripts/audit_limit_price_coverage.py` | 最新日交集 **100%**（345/345）；全市场仅 345 只有当日行情属数据截止现象，非 limit 表缺失 |

**仍留局限**：

- 批量模拟 **未**自动串 `suggest_rebalance` 权重再算股数（optimizer 仍只影响 rebalance 动作语义）。  
- 建议日收盘 ≠ 次日开盘价；S7 偏差表已标注口径。  
- M17 已通过 `close_warehouse` 覆盖全部 Streamlit 子页。

```powershell
.venv\Scripts\python.exe scripts\test_paper_batch_simulate.py
.venv\Scripts\python.exe scripts\audit_limit_price_coverage.py
```

---

## 7. 投产补完（2026-09-17）

对照「真实环境自用」梳理的待办与本轮交付：

| 优先级 | 待办 | 状态 | 交付 |
|--------|------|------|------|
| P0 | 日常决策链文档（数据更新 + 收盘流程） | **已补** | `docs/production-daily-runbook.md` |
| P0 | 行情滞后 / 灌库只读可见 | **已补** | `render_production_banners`（首页、持仓页） |
| P0 | M17 全站合规页脚 | **已确认** | `close_warehouse` 统一调用 `render_trust_footer` |
| P0 | 一键练习闭环（扫描→建议→模拟） | **已补（本地）** | `run_practice_cycle` + 持仓页按钮 |
| P1 | S4 置信度文案 | **已补** | 卡片 caption「分位不是胜率」 |
| P1 | 规格 §6 与 README/HANDOFF 滞后 | **已同步** | 本报告 + `9.16-需求规格` §6 |
| P2 | Should：S1 理由包 / S2 行业上限 / S9 计划仓位 | **已补（2026-09-18）** | 见 §8–§9；S2 非 C3 全中性 |
| P2 | 独立 S6 周报页 | **未做** | expander 摘要已够用 |
| P2 | optimizer→open 股数全自动 | **未做** | 工程决策：rebalance 语义优先 |
| P2 | Word 手册与 Streamlit 对齐 | **未做** | 以 Markdown 日课为准 |
| 数据 | `index_weight` 历史、北交所 eastmoney | **运维** | 见 data-update 复盘 / 网络说明 |

**投产前建议跑**：日课 §4 脚本 + 人工可用性测试指南勾一遍。

---

## 8. M16 全链路（2026-09-18）

对照规格原文「至少一条 advice_id 全链路」与 `docs/phase5-gap-construction.md` §1。

| 项 | 结果 |
|----|------|
| 模块 | `advice/advice_trace.py`（页面只调用） |
| 证据 | `scripts/test_advice_trace.py`：1 条完整 trace；`fill_shares==planned_shares`；`fill_vs_advice_close=-4.67%`（次日开盘相对信号收盘，执行缺口口径） |
| 已知局限 | 无 Level-2，IS 不分解冲击；生产库成交条数仍可为 0 |

## 9. S1 / S2 / S9（2026-09-18）

对照 `docs/phase5-gap-construction.md` §2–§4。

| ID | 证据 | 局限 |
|----|------|------|
| S1 | `scripts/test_reason_pack.py`：冲突进入 reasons；`attach_reason_packs` 不改 rank | 行业中文名仍缺 |
| S2 | `scripts/test_industry_cap.py`：同行业第二只 open 不进待办；止损不拦截 | 不是 Barra 全中性；无行业代码则不占名额 |
| S9 | 与 M16 同测：`shares_gap==0`；复盘页台账 | 未成交不入台账（有意：避免假成交） |

*冠军模型未替换；scanner 缺 `champion.json` 时失败，不读最新文件夹。*

---

## 10. 规格再核对与探针循环（2026-09-18）

对照用户「逐项核对 → 补齐 → 全流程探针 → 归档 → 再修」。

| 项 | 结果 | 证据 |
|----|------|------|
| M8 watch/hold 空话 | **已修** | `actionable_invalid_if_watch/hold`；缺止损 open→watch；`test_m8_invalid_if.py` |
| M4 UI T+1 | **已补** | `pages/1` 模拟盘 caption |
| M1 只读冠军 | **已收紧** | 无 `champion.json` 抛错；`test_champion_only.py` |
| S1 跨会话 | **已补** | `advice_log.reason_one_liner` 落盘/回放 |
| 审计退出码 | **已拆** | `audit_phase5` 默认代码/schema 失败才 exit 1；生产未就绪记 DATA_BLOCKED |
| 全流程探针 | **已跑** | `scripts/probe_retail_chain.py` → `docs/phase5-probe-log-20260918.md` |

**生产库**：`limit_price`/`daily_basic` 覆盖 0、有效截面 2026-09-17 vs 期望 2026-09-18；灌库保持暂停，决策 UI 继续阻塞。完整 `advice_id` 成交 trace 生产库仍为 0（用户未模拟/M15）。
