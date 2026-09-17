# Phase 5 开发文档 —— 完整散户决策链（模拟盘 → 可执行建议）

> **对照**：`docs/9.16-散户决策链需求规格.md` 的 Must；理论：`../../docs/00-总览/08-第四轮散户决策链补齐对照.md`。  
> **约束**：同目录 `phase5-constraints.md`（先读约束再写代码）。  
> **阶段草案**：`retail-complete-phase5-handoff.md`。  
> **本文件不授权跨阶段一次改完。**

## 1. 一句话

在不接券商、不杀当前灌库进程的前提下，按 A→B→C→D 把纸面账户、股数、次日待办、复盘做成可测模块；规则函数优先复用 `research/a_share_rules.py`。

## 2. 理论 → 模块（ADR 摘要）

| 选型 | 为什么选 | 放弃什么 |
|------|----------|----------|
| 纸面账户在 DuckDB 内、与 `positions` 分表 | 单写者、可复盘、符合需求 M14 | sidecar 库、券商模拟盘 API |
| 成交默认次日开盘 | 与收盘后待办作息一致、可回测 | 默认次日收盘；真实盘口 |
| 费用调用 `apply_costs` | 与 Phase 1 回测同一套，避免两套印花税 | 在 broker 里硬编码新税率 |
| 以损定仓 + 手数取严 | `../../docs/04-风险管理/10-以损定仓与A股手数.md` | 只输出权重让用户自己换算 |
| `entry_rules` 纯函数 | 引擎与回测共用（分层：research/advice 纯函数） | 页面里写开盘价 if |
| 择时验收看成交率与成本差 | `../../docs/03-量化方法/15-日频信号与次日执行.md`、中国账户择时项为负 | 「择时后收益变好才算过」 |

## 3. 阶段与 Must 映射

| 阶段 | Must | 建议路径 |
|------|------|----------|
| A 模拟盘内核 | M3 部分、M4、M9、M10、M14、M19 | `common/db.py` 扩表；`advice/paper_broker.py`；CLI 或「持仓与建议」一键模拟 |
| B 可执行建议 | M5–M8、M12、M13、M17 文案、M18 测试化 | `advice/advice_engine.py`；`advice_log` 增列；UI 卡片 |
| C 择时待办 | M11 | `advice/entry_rules.py`；明日待办 UI；`scripts/backtest_entry_rules.py`（验收后若一次性则删除，回归则保留） |
| D 复盘实操 | M15、M16、S6–S7 | `pages/7_历史建议复盘.py` 升级；确认成交 |

S1–S5、S8–S9 不阻塞 A。

## 4. 表结构（落地时与 `init_schema` 一致）

与对接文档草案对齐，实现时二选一写进验收报告：

- `paper_account(account_id, template_id, initial_cash, created_at, note)`  
  模板：`practice_100k` / `live_prep_10k`  
- `paper_positions(account_id, symbol, shares, avg_cost, opened_at, …)` 或保留 lot  
- `paper_trades(trade_id, account_id, advice_id, symbol, side, shares, price, fees, trade_date, source, reject_reason)`  
  `source ∈ {sim, user_confirmed_live}`  
- 现金：由 trades 滚动 **或** `paper_cash` 快照，**只选一种**  
- 可选 `paper_nav_daily`

开发 DDL：**临时 DuckDB** 或等 `warehouse.duckdb` 写者结束。禁止为开发打断 `run_data_update`。

## 5. 模块职责

| 模块 | 做 | 不做 |
|------|----|------|
| `paper_broker` | 下单校验、成交、记账、幂等 | HTTP、猜涨跌停字段 |
| `a_share_rules` | 费用、涨跌幅阈值 | 摸库 |
| `advice_engine` | 卡片动作 + 股数字段 | 自己算印花税 |
| `entry_rules` | 信号日/执行日/失效 | 写库 |
| `portfolio_optimizer` | 目标权重 | 直接吐股数（股数在 B 用净值换） |
| 页面 | 调用上述模块 | 复制公式 |

涨跌停/停牌：查已有 `limit_price`、`suspend_calendar`、当日 `daily_quotes`，字段以 ingestion 文档为准，禁止臆测列名。

## 6. 模拟成交伪代码（语义，不是复制粘贴开工）

```text
for order in plan:
  if not tradable(symbol, exec_date): reject("停牌或涨跌停")
  if buy and shares % 100 != 0: reject("手数")
  if sell and shares > sellable_tplus1: reject("T+1")
  fees = apply_costs(notional, side, exchange)
  if buy and cash < notional + fees: reject("现金不足")
  if already_filled(advice_id): skip  # 幂等
  fill at open(exec_date) ± slippage_bp
  write paper_trades; update positions/cash
```

## 7. 测试（闸门 4）

- `scripts/test_paper_broker.py`：T+1、100 股、涨停拒买、最低佣金 5 元、北交所无印花税、幂等。  
- 1 万买不起 1 手 → `watch`（可扩 `test_concentration.py` 思路）。  
- 浮亏持仓不得出现 `add`。  
- 证据：临时库查询净值 = 现金 + 市值；重复跑成交行数不变。  
- UI：改完重启 Streamlit；Playwright 或人工走「生成计划 → 模拟 → 净值变化」。

## 8. 验收文档

新建 `docs/phase5-acceptance-report.md`，逐条对照需求规格 §5 与对接 §5。已知上限写进「局限」：滑点非盘口、择时不承诺超额。

## 9. 文件清单（实现时）

| 路径 | 阶段 |
|------|------|
| `common/db.py` schema | A |
| `advice/paper_broker.py` | A |
| `config.yaml` `paper_trading` | A |
| `advice/advice_engine.py` 股数 | B |
| `advice/entry_rules.py` | C |
| `pages/1_持仓与建议.py` 或新页「明日待办」 | B/C |
| `pages/7_历史建议复盘.py` | D |
