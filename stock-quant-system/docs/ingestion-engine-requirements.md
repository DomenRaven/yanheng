# 数据更新引擎 — 需求说明（闸门 1–2）

## 1. 一句话目标

提供专用程序 `scripts/run_data_update.py`，在遵守 DuckDB 单写者与现有 ingestion 语义的前提下，通过 **并发取数 + 有界队列背压 + 异步写线程** 缩短全市场增量/补抓耗时，并降低全量首跑时内存堆积风险。

## 2. 功能需求

| ID | 需求 | 优先级 |
|----|------|--------|
| R1 | CLI 支持与 `daily_pipeline` 相同的步骤集合（`--only` / `--skip` / limit 参数） | P0 |
| R2 | 单步内 fetch 与 DB 写入解耦：写入仅在专用写线程执行 | P0 |
| R3 | 有界队列：`maxsize = f(http_workers)`，队列满时 fetch 侧阻塞（背压） | P0 |
| R4 | Tushare 路径 `workers=1` 时仍能通过写线程重叠「下一次 HTTP」与「上一次 upsert」 | P0 |
| R5 | HTTP 多工人路径（quotes/fundamentals 等）继续复用 `IntervalLimiter`，不突破全局限速 | P0 |
| R6 | 单步失败不阻塞其余步骤；失败信息进入返回汇总与 `sync_log`（沿用各 ingestion 模块） | P0 |
| R7 | 配置项：`ingestion.pipeline.async_write`、`write_queue_depth`（可选 `write_batch_size` 预留） | P1 |
| R8 | `daily_pipeline.py` 可委托同一编排器，避免双份步骤逻辑 | P1 |

## 3. 非功能需求

| ID | 需求 |
|----|------|
| NF1 | 不得在工作线程内长时间 `get_connection()` 做 HTTP（延续 `write_session` 纪律） |
| NF2 | Windows 下与 Streamlit 只读共存：写路径仍走 `_WRITE_LOCK` + 短连接 |
| NF3 | 默认行为可回退：`async_write: false` 时与旧 `map_fetch_then_write` 语义一致（写回调在调用线程） |

## 4. 不在本期范围

- 改写 `sync_bse_cdr_qfq_quotes` / `sync_index_weight` 等「循环内长连接」模块（记入后续任务）。
- 多机分布式抓取、Bronze 原始层、替换 Tushare 为多工人（仍非线程安全，保守 `workers=1`）。
- 对生产 `warehouse.duckdb` 的全市场压测（避免与正在跑的 pipeline 抢锁）。

## 5. 验收标准（闸门 5 原文）

见 `docs/ingestion-engine-acceptance-report.md` 检查表；沙箱脚本 `scripts/test_ingestion_engine.py` 全部通过。
