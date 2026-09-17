# 全量数据更新复盘（2026-09-16/17）

**run_id**：`20260916-150917-c62d0993` · **结果**：10/10 步 `ok` · **状态文件**：`data/ingestion_pipeline_state.json`

## 1. 用了多久

| 口径 | 时间 | 说明 |
|------|------|------|
| **日历跨度** | 约 **12h 25m** | 15:09（16 日）→ 03:34（17 日） |
| **实际跑机** | 约 **10h 45m** | 第一段 ~6h 12m（至中断）+ 续跑 ~4h 32m（271.8 min） |
| **理想单会话墙钟** | 约 **8h 50m** | 三步财务并行取最长波次，不重跑、不中断（见下表合计） |
| **续跑段 alone** | **271.8 min** | 仅 `tushare_market_data` + `tushare_behavior` |

各步 `elapsed_s`（状态文件，并行步为各自 CPU 时间，非墙钟叠加）：

| 步骤 | 秒 | 分钟 | 备注 |
|------|-----|------|------|
| universe | 178 | 3.0 | |
| quotes | 5114 | 85.2 | |
| fundamentals ∥ income ∥ corporate | 6210 / 3982 / **9009** | 墙钟 ≈ **150**（取 corporate） | 并行波 |
| reference_data | 156 | 2.6 | |
| market_data | 43 | 0.7 | |
| tushare_prices | 4012 | 66.9 | |
| tushare_market_data | 5143 | 85.7 | 含 daily_basic 全量逐股 |
| tushare_behavior | 11167 | **186.1** | 最慢一步 |

**中断损耗**：第一次在 `sync_daily_basic` 约 **78%** 处停机；`--resume` 整步重跑 `tushare_market_data`，约 **多耗 40–65 min** 等效 API（upsert 不脏数据，但重复 HTTP）。

## 2. 工作流程复盘

| 环节 | 本次做法 | 问题 |
|------|----------|------|
| 启动 | `--fresh-run` + 日志 `run_data_update_20260916_full.log` | 合理 |
| 监测 | `watch_data_update --interval 5` | 合理；早期 ETA 低估 behavior |
| 暂停 | `Stop-Process`，未 SIGINT | 断点 JSON 需手改 `interrupted`；步内无子任务断点 |
| 续跑 | `--resume`，参数一致 | 8 步跳过正确；**market_data 整步重跑** |
| 验收 | 流水线 `completed` | **`index_weight` 约 100 个月度点失败**（500/1000 后半段 burst 无 limiter） |

## 3. 优化项（优先级）

### P0 — 已实施（2026-09-17）

1. **`sync_index_weight` → `run_item_loop_sync` + `tushare_limiter()`**（消除 burst 超限）
2. **`sync_daily_basic` 按 `trade_date` 全市场快照 + `max(trade_date)` 增量**
3. **步内 `substeps` 断点**（`tushare_market_data` 四子任务；整步 ok 后清除）

补跑指数缺口：`python -m ingestion.tushare_market_data --task index_weight`

### P1 — 后续

1. **`tushare_behavior` 步内 substeps**（与 market_data 同模式）
2. **日更默认** `--skip fundamentals,income_statement,corporate_actions`
3. **全量前** 关 Streamlit 写库；续跑禁止改 `--only`/`--skip`/`*-limit`

## 4. 建议的日常命令

```powershell
cd stock-quant-system
# 日更（示例）
.venv\Scripts\python.exe -m scripts.run_data_update --skip fundamentals,income_statement,corporate_actions

# 补 index_weight 缺口
.venv\Scripts\python.exe -m ingestion.tushare_market_data --task index_weight
```

## 5. 已知局限（诚实存疑）

- 步 `elapsed_s` 为进程内计时，并行波次不能简单相加当墙钟。
- Tushare 多工人仍保守 `workers=1`；提速主要靠 **API 切面上按日批量** 与 **少重跑**，而非盲目加线程。
