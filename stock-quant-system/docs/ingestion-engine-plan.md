# 数据更新引擎 — 开发计划

## 阶段 A（本期）

1. 文档：research / requirements / constraints / plan（本批文件）
2. 框架：`common/ingestion_engine/bounded_pipeline.py`、`config.py`、`orchestrator.py`
3. 接入：`parallel_fetch.map_fetch_then_write` 在 `async_write=true` 时委托 bounded 实现；`tushare_client.run_*_loop_sync` 默认走 async 写
4. 入口：`scripts/run_data_update.py`；`daily_pipeline.py` 薄封装调用 orchestrator
5. 测试：`scripts/test_ingestion_engine.py` + 保留 `test_parallel_fetch`（`async_write=false`）
6. 验收报告 + 工作报告

## 阶段 B（后续，不在本期开工）

- 将 `sync_bse_cdr_qfq_quotes`、`sync_index_weight` 改为 fetch-then-`write_session`
- `write_batch_size > 1` 的同表 micro-batch upsert
- 可选：quotes 与 reference_data 时间重叠（需全部模块短连接化）

## 文件清单

| 路径 | 作用 |
|------|------|
| `common/ingestion_engine/bounded_pipeline.py` | 有界队列 + 写线程 |
| `common/ingestion_engine/config.py` | 读取 pipeline 配置 |
| `common/ingestion_engine/orchestrator.py` | 步骤编排 |
| `scripts/run_data_update.py` | 用户入口 |
| `scripts/test_ingestion_engine.py` | 沙箱验收 |
