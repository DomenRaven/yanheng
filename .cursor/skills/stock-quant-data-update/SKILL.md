---
name: stock-quant-data-update
description: >-
  Runs and resumes the stock-quant-system DuckDB data pipeline (run_data_update),
  checkpoint/resume, E2E tests, and production full/incremental updates. Use when
  the user asks to update data, 数据更新, daily_pipeline, run_data_update, 断点续跑,
  or ingestion for this 炒股辅助 / stock-quant-system project.
---

# 股票量化项目 — 数据更新

工作目录：`stock-quant-system/`。遵守 `yanheng-dev-loop` 与 DuckDB 单写者（更新期间尽量少开 Streamlit 写库）。

## 入口

```powershell
cd stock-quant-system
$venv = ".venv\Scripts\python.exe"

# 全量/首次灌库（新 run_id）
& $venv -m scripts.run_data_update --fresh-run

# 中断后续跑（参数须与上次一致：only/skip/limit）
& $venv -m scripts.run_data_update --resume

# 日常增量（可跳过低频财务步）
& $venv -m scripts.run_data_update --skip fundamentals,income_statement
```

断点文件：`data/ingestion_pipeline_state.json`（与 `warehouse.duckdb` 同目录）。

## 断点语义（回答用户「不重复不缺漏」）

| 层级 | 机制 |
|------|------|
| **流水线** | `--resume` 跳过本 `run_id` 下 `status=ok` 的**整步**；中断的步未标 ok，续跑会重进该步。 |
| **步内** | `ingestion_pipeline_state.json` 的 **`substeps`**（如 `tushare_market_data.daily_basic`）在 `--resume` 时跳过已完成子任务；整步成功后清除。 |
| **表内** | 各 `ingestion/*` 按 `max(trade_date)` / 报告期 / 主键 **upsert** 增量。 |
| **审计** | `sync_log` 记录逐股/逐日成败。 |

暂停：在跑 `run_data_update` 的终端 **Ctrl+C**（SIGINT），会写 `status=interrupted`；勿对进程强杀。

不要用 `--resume` 搭配与断点不同的 `--only`/`--skip`/`*-limit`；改编排请 `--fresh-run`。

## E2E / 调试

```powershell
$env:STOCK_QUANT_DB = (Resolve-Path data\e2e_test.duckdb).Path
& $venv -m scripts.run_data_update --fresh-run --quotes-limit 5 --fundamentals-limit 2 --corporate-actions-limit 2 --tushare-limit 3
& $venv scripts\test_ingestion_engine.py
& $venv scripts\test_reference_disclosure.py
& $venv scripts\test_parallel_fetch.py
```

## 耗时量级（无 limit）

- 轻量步（universe + reference + market）：约 3–5 分钟  
- 日常增量（跳过财务）：约 1–3 小时  
- 首次全量 10 步：约 10–20+ 小时，宜后台 + 日志  

## Agent 执行清单

1. 确认 `.env` 含 `TUSHARE_TOKEN`；未要求时不要设 `STOCK_QUANT_DB`（用生产 `data/warehouse.duckdb`）。
2. 全量：`--fresh-run`，后台运行并 `Tee-Object` 到 `data/logs/run_data_update_<date>.log`。
3. 用户说「接着更新」：`--resume`，禁止改 only/skip/limit。
4. 完成后读 `_meta` / 状态文件；失败步查 `sync_log`。
5. 文档：`docs/ingestion-engine-work-report.md`、`docs/ingestion-engine-requirements.md`。

## 禁止

- 提交 `.env`、`*.duckdb`、API Key  
- 无 `--tushare-limit` 的「假 E2E」当全量验收  
- sidecar 双库写主仓  
