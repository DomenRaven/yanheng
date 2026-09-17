# 数据更新引擎 — 验收报告（闸门 5）

对照：`docs/ingestion-engine-requirements.md` 第 5 节、`docs/ingestion-engine-constraints.md`。

验收日期：2026-09-15（沙箱环境，未对占用中的 `data/warehouse.duckdb` 做全市场实盘压测）。

## 检查表

| ID | 条款 | 结果 | 证据 |
|----|------|------|------|
| R1 | CLI 步骤与 limit 参数 | 通过 | `scripts/run_data_update.py` + `orchestrator.STEP_ORDER` |
| R2 | 写仅在专用写线程 | 通过 | `test_writer_runs_on_dedicated_thread` |
| R3 | 有界队列背压 | 通过 | `test_backpressure_limits_queue` |
| R4 | workers=1 时 fetch/写重叠 | 通过 | `test_fetch_write_overlap_while_writer_busy` |
| R5 | 全局限速仍经 `IntervalLimiter` | 通过 | 实现未改 limiter；`test_parallel_fetch.test_limiter_global_interval` |
| R6 | 单步失败不拖垮管道 | 通过 | `orchestrator._run_one_step` try/except（沿用原逻辑） |
| R7 | `config.yaml` pipeline 配置 | 通过 | `ingestion.pipeline.async_write` / `write_queue_depth` / `write_batch_size` |
| R8 | `daily_pipeline` 委托编排器 | 通过 | `scripts/daily_pipeline.py` → `run_data_update` |
| NF1 | 工人线程不写库 | 通过 | 设计约束 + 代码审查 `bounded_pipeline.py` |
| NF2 | `write_session` + `_WRITE_LOCK` | 通过 | `scripts/test_duckdb_lock` ALL PASSED |
| NF3 | `async_write: false` 回退旧语义 | 通过 | `test_legacy_map_fetch_on_caller_thread_when_async_disabled` |

## 测试命令（闸门 4）

```powershell
cd stock-quant-system
.venv\Scripts\python.exe -m scripts.test_ingestion_engine
.venv\Scripts\python.exe -m scripts.test_parallel_fetch
.venv\Scripts\python.exe -m scripts.test_duckdb_lock
```

2026-09-15 本机执行：上述三项均通过。

## 已知局限（闸门 7）

- 未在「主库被长任务占用」时做 `--limit` 微基准；与 `parallel-http-ingestion.md` 一致，待下次空闲窗口补跑。
- `sync_bse_cdr_qfq_quotes`、`sync_index_weight` 等仍在循环内长连接，本期未改。
- **2026-09-16 修复**：`reference_data` 披露日历巨潮 403→`ingestion/reference_data.py` 补请求头 + Tushare `disclosure_date` 回退 + 连续失败熔断；回归 `scripts/test_reference_disclosure.py`。
- `write_batch_size>1` 仅配置占位，逻辑未实现。
- `async_write=true` 时 `on_result` 不在主线程；`test_parallel_fetch` 中线程契约用例在强制 `async_write=false` 下运行。

## 结论

**本期验收通过**：专用入口与 fetch/write 解耦引擎已落地，沙箱门禁满足需求文档 P0/P1 条款；全市场生产耗时对比留作下一迭代证据。
