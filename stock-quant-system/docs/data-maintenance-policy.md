# 数据维护策略（日更 / 周更 / 启动自检）

> **对照**：`docs/9.16-散户决策链需求规格.md`（日频决策链、非 tick）；`config.yaml` → `production_readiness`；理论库 `docs/05-工具库/02-数据源.md`（PIT / 质量清单）、`docs/03-量化方法/10-时序验证与标签工程.md`（Walk-Forward、样本外纪律）。

---

## 1. 设计原则（理论验证）

| 原则 | 依据 | 在本项目中的落地 |
|------|------|------------------|
| **决策频率 = 数据频率** | 散户链为收盘后日频扫描/建议，非高频；Barber 等「过度交易」研究针对的是执行频率，不要求 tick（见需求规格 §2 文献链） | 不接入同花顺/券商「用时再拉」作为默认；**收盘后灌库 + 有效截面日** 即「新鲜」 |
| **Point-in-time（PIT）** | 理论库 §3 质量清单：财务用公告/可得日，不用报告期当日 | `research/panel.py` 宽表带 `effective_date`；披露日历见 `reference_data` |
| **可交易性截面** | 涨跌停、停牌、T+1 需官方或可复现字段 | `limit_price`、`suspend`、交易日历 → `tushare_market_data` / `market_data` |
| **避免「假新」** | 少数股票有新线而全市场未对齐会误导扫描 | `effective_market_quote_date`：≥80% 活跃 universe 有 qfq 的最近一日（与 `panel.build_asof_snapshot` 同口径，`production_readiness.min_quote_coverage_ratio: 0.80`） |
| **标签与重训节奏** | Walk-Forward / Purged K-Fold 要求历史一致、重训有固定 cadence，而非每天改特征全集 | `model.retrain_cadence: weekly`；**日更**沪深 quotes + 北交所轻量；**周更**财务/行为/全量复权 + 周末重训 |
| **工程可完成性** | 免费源不稳定时，应用「熔断 / 分频 / 换源」而非无限重试 | 北交所 AkShare 东财路径易 **ProxyError**；日更 **跳过 bj 逐股东财**，改走轻量步 `tushare_bse_quotes`；周末 `tushare_prices` 全量复权+退市+北交所 |

**验收口径（可复现）**：每次维护后运行 `python -m scripts.check_warehouse_readiness`，退出码 0；关注 `effective_quote_date` 与最近交易日 lag ≤ `max_quote_lag_trading_days`（默认 2）。

---

## 2. 流水线步骤与数据类别

编排顺序见 `common/ingestion_engine/orchestrator.py` → `STEP_ORDER`。

### 2.1 工作日 — `weekday_decision`（收盘后或零点）

**命令**：

```powershell
cd stock-quant-system
.venv\Scripts\python.exe -m scripts.run_daily_refresh --profile weekday_decision
```

| 步骤 | 主要表 / 内容 | 用途（决策链 / 研究） |
|------|----------------|------------------------|
| `universe` | `universe` 上市/退市/板块 | 扫描宇宙、覆盖率分母 |
| `quotes_decision` | 仅 **持仓 / 模拟持仓 / 近 7 日建议** 标的的量价；沪深走 AkShare，bj 走 Tushare（强制刷 adj） | 持仓决策、风险、复盘、待办模拟 |
| `market_data` | 交易日历、指数等参考行情 | `asof` 对齐、lag 计算 |
| `tushare_market_data` | **全市场** `limit_price`、`daily_basic`、停牌等 | 可交易过滤、因子依赖（一日一张截面，通常较快） |

**刻意不在工作日跑的步骤**：

- 全市场 `quotes` / `tushare_bse_quotes` — **周更**（掘金需要全市场截面）
- `fundamentals` / `income_statement` / `corporate_actions` / `tushare_prices` / `tushare_behavior` / `reference_data` — **周更**

**耗时量级**：持仓个位数～几十只时通常 **数分钟级**（含全市场 limit/basic）；不再默认扫 5000+ 日 K。

### 2.2 周末 — `weekend_research`（模型 / 因子 / 补北交所）

**命令**：

```powershell
.venv\Scripts\python.exe -m scripts.run_daily_refresh --profile weekend_research
```

| 步骤 | 主要表 / 内容 | 用途 |
|------|----------------|------|
| `quotes` | 全市场沪深日 K（AkShare；bj 仍跳过东财） | 掘金截面、有效报价日 |
| `fundamentals` | EAV 财务指标 | 价值/质量/成长因子，PIT 宽表 |
| `income_statement` | 利润表 EAV | 同上 |
| `corporate_actions` | 分红送转等 | 与复权、事件标签一致 |
| `reference_data` | 披露日、行业、ST 映射等 | PIT、中性化 |
| `tushare_prices` | 复权因子、退市 raw、**北交所/CDR qfq**（`refresh_adj=True`） | 全量补齐；与日更轻量步同路径，避免「旧 adj 对不齐新日线」 |
| `tushare_market_data` | 与工作日相同源，可补历史缺口 | 维持 limit/basic 覆盖 |
| `tushare_behavior` | 行为类 Tushare 表 | 扩展因子 / 研究（非 Must 阻塞项） |

**不重复**：周末 **含** 全市场 `quotes`（工作日已改为持仓增量）；若只想补财务可自行 `--skip quotes`。

**配合**：`model.retrain_cadence: weekly` → 周末灌库完成后再跑 MLOps 重训（见 `mlops/retrain_schedule.py`、Phase 2 验收报告）。

### 2.3 其它预设（保留）

| Profile | 场景 |
|---------|------|
| `bootstrap` | 空库首次：`--fresh-run` + 跳过 `quotes_decision`/`tushare_bse_quotes`，宜过夜 + 日志 |
| `incremental` | 旧日常（仍含全市场 quotes 东财 bj 风险）— **不推荐默认** |
| `decision_min` | 与 weekday 同四步（`quotes_decision`） |
| `index_weight_gap` | 仅补 `index_weight` 历史 |

**首次灌库注意**：若需要北交所 AkShare 全量，可临时清空 `config.yaml` → `ingestion.quotes_daily_skip_exchanges`，或依赖周末 `tushare_prices` 覆盖 bj。

---

## 3. 维护方式（自动化 + 本机启动）

### 3.1 单写者与断点

- **唯一写者**：`run_data_update` / `run_daily_refresh` 运行期间 DuckDB 写锁；Streamlit 决策页只读或阻塞。
- **断点**：`data/ingestion_pipeline_state.json`；中断用 **Ctrl+C**（写 `interrupted`），续跑 `python -m scripts.run_data_update --resume`（**勿改** `--only`/`--skip`/`*-limit`）。
- **禁止**：并行两个灌库进程（曾导致 quotes 重复、整夜占用）。

### 3.2 Windows 计划任务（仓库一键注册）

在本机 **执行一次**（当前用户，通常无需管理员）：

```powershell
cd stock-quant-system\scripts
powershell -NoProfile -ExecutionPolicy Bypass -File .\install_windows_maintenance_tasks.ps1
```

| 注册任务名 | 默认触发 | Profile |
|------------|----------|---------|
| `StockQuant-WeekdayDecision` | 周一至五 16:15 | `weekday_decision` |
| `StockQuant-MidnightCatchup` | 每天 00:30 | `weekday_decision` |
| `StockQuant-WeekendResearch` | 周六 09:00 | `weekend_research` |

- 日志：`data/logs/scheduled_<profile>_*.log`（由 `scripts/invoke_scheduled_refresh.ps1` 写入）
- 卸载：`powershell -File scripts\uninstall_windows_maintenance_tasks.ps1`
- 详表：`docs/manuals/windows-scheduled-maintenance.md`

**可选** 登录补灌（未就绪则 `ensure_data_fresh --apply`，与 Streamlit 勿并行写库）：

```powershell
powershell -File .\scripts\install_windows_maintenance_tasks.ps1 -IncludeLogonAutoApply
```

手动配置（不跑安装脚本）仍可用任务计划程序，参数见 `docs/production-daily-runbook.md` §2。

### 3.3 本机启动时（不遗漏）

1. **CLI（启动器 / 登录脚本）**：

   ```powershell
   python -m scripts.check_startup_data
   ```

   等价于就绪检测 + 若未就绪且未在灌库则提示 `ensure_data_fresh --apply` 将用的 profile（`auto`：工作日 → `weekday_decision`，周末 → `weekend_research`，空库 → `bootstrap`）。

2. **打开 Streamlit 时**：侧边栏已缓存 `assess_warehouse_readiness`；未就绪红条 + `ensure_data_fresh --apply` 说明（见 `production-daily-runbook.md` §1）。

3. **监测进行中任务**：

   ```powershell
   python -m scripts.watch_data_update --interval 30
   ```

### 3.4 北交所（bj）专项

- **日更东财跳过**：`ingestion.quotes_daily_skip_exchanges: [bj]` → `sync_quotes_batch` 统计 `skipped_exchange_policy`，**不调用**东财 `_fetch_em_hist`。
- **日更 Tushare 轻量补**：`weekday_decision` / `decision_min` 含步骤 `tushare_bse_quotes` → `sync_bse_cdr_qfq_quotes(refresh_adj=True)`（在市 bj + 科创板 CDR `689009`）。
- **周更 / 全量**：`tushare_prices` → `sync_all_tushare_prices`（复权因子全集 + 退市 raw + 同上 bj/CDR，同样 `refresh_adj=True`）。`bootstrap` **跳过**轻量步，避免与 `tushare_prices` 重复。
- **关键经验（2026-09-18）**：库内已有旧 `adj_factor` 但缺最近交易日时，若只在 `adj.empty` 时才拉复权因子，会出现「复权因子无法对齐日线」、整批 `rows_written=0`。**补缺口前必须强制刷新该票复权因子再转 qfq**；刷新失败且库内仍有旧因子时才回退旧值。
- **投产**：扫描宇宙含 bj 时，工作日轻量步即可对齐最近交易日；周末 `weekend_research` 再做全市场复权与退市补齐。`effective` 截面分母为全 universe，bj 长期缺失会拉低覆盖率。

### 3.5 故障处理

| 现象 | 处理 |
|------|------|
| `interrupted` 仅 universe ok | `--resume`，skip 与上次一致 |
| quotes 大量 failed（非 bj） | 查 `sync_log`、网络；勿开第二个 full incremental |
| reference 巨潮熔断 | 已 `consecutive_fail_abort`；可 `prefer_tushare: true` 或周末再试 |
| ready 但 warning naive vs effective | 继续跑到 `effective` 对齐最近交易日 |
| 需要只补 limit/basic | `--only tushare_market_data` 或 `decision_min` |
| 北交所 qfq 写 0 行 / AI 分停滞在旧月 | 确认走 `tushare_bse_quotes` 或 `tushare_prices`（强制刷 adj）；勿只靠东财 bj |

---

## 4. 与「用时再拉券商/同花顺」的边界

- **默认不做**：接口未接、合规与稳定性未定；决策链依赖 **本地 DuckDB 可复现截面**。
- **LLM 新闻层**：`llm/news_fetch` 为解释用即时抓取，**不**替代行情/涨跌停灌库。
- 若未来接券商持仓，仍建议 **行情/规则表走本维护策略**，持仓仅镜像录入（需求规格 Must 分离）。

---

## 5. 文档与入口索引

| 入口 | 说明 |
|------|------|
| `scripts.run_daily_refresh` | 预设 profile（**推荐**） |
| `scripts.run_data_update` | 底层编排，支持 `--only` / `--skip` / `--resume` |
| `scripts.ensure_data_fresh` | `--apply` 后台 spawn；`--profile auto` |
| `scripts.check_warehouse_readiness` | 投产硬门槛 |
| `scripts.check_startup_data` | 启动自检 + 建议命令 |
| `docs/production-daily-runbook.md` | 日课清单 |
| `.cursor/skills/stock-quant-data-update/SKILL.md` | Agent 灌库规程 |

---

## 6. 已知局限（诚实存疑）

- **AkShare 东财 bj**：受本机代理/VPN 影响大；日更跳过 bj 东财、改走 Tushare 轻量步是工程权衡，非理论最优「全市场同源同日截面」。
- **`tushare_behavior` 周更**：部分 Must UI 不依赖；若未来 Must 化需单独评估耗时与积分。
- **计划任务依赖本机开机**：00:30 catch-up 不能替代云侧调度；长期离线需手动 `weekday_decision`。
- **incremental 旧 profile** 仍含 bj 东财；文档与计划任务应改用 **`weekday_decision`**，避免回归「拖死整晚」。
- **轻量步与全量步重叠**：`tushare_bse_quotes` ⊂ `tushare_prices` 的 bj 子任务；同日勿并行两个写者。

*文档版本：2026-09-18，与 `tushare_bse_quotes` 日更步及 `refresh_adj` 强制刷新经验同步。*
