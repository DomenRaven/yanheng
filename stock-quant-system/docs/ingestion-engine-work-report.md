# 数据更新引擎 — 工作报告

## 1. 任务与对照条款

- **需求**：替代「线性逐条写库」，采用并发取数 + 异步写入，提升更新效率与稳定性。
- **对照文档**：`ingestion-engine-requirements.md`（R1–R8、NF1–NF3）。
- **道德/流程**：按八闸门完成调研 → 设计 → 实现 → 沙箱测试 → 验收 → 留痕。

## 2. 行业调研摘要

见 `ingestion-engine-research.md`。核心结论：在 DuckDB 单写者约束下，业界普遍采用 **多 fetch + 有界队列 + 单写消费者**，而非多进程写同一文件或 sidecar 双库。

## 3. 交付物

| 类型 | 路径 |
|------|------|
| 调研 | `docs/ingestion-engine-research.md` |
| 需求 | `docs/ingestion-engine-requirements.md` |
| 约束 | `docs/ingestion-engine-constraints.md` |
| 计划 | `docs/ingestion-engine-plan.md` |
| 验收 | `docs/ingestion-engine-acceptance-report.md` |
| 引擎代码 | `common/ingestion_engine/` |
| 入口 | `python -m scripts.run_data_update` |
| 兼容入口 | `python -m scripts.daily_pipeline`（同一编排器） |
| 沙箱测试 | `scripts/test_ingestion_engine.py` |

## 4. 实现要点

1. **`bounded_map_fetch_then_write`**：`queue.Queue(maxsize=depth)` + `ingestion-writer` 线程；fetch 池与写库解耦。
2. **`parallel_fetch.map_fetch_then_write`**：当 `ingestion.pipeline.async_write=true`（默认）时委托上述实现；`false` 保持 2026-08-27 行为。
3. **Tushare 逐股/逐日循环**：经 `map_fetch_then_write` 自动获得写线程重叠（`workers=1` 时收益最明显）。
4. **配置**：`config.yaml` 新增 `ingestion.pipeline` 段。

## 5. 测试与验收

- `test_ingestion_engine`：写线程、背压、重叠、错误计数、回退模式。
- `test_parallel_fetch` / `test_duckdb_lock`：回归通过（编排测试改为 patch `orchestrator.STEP_FUNCS`）。
- 全市场实盘压测：**未做**（避免与可能占用主库的 pipeline 抢锁）。

## 6. 后续建议（闸门 8）

1. 空闲窗口对 `run_data_update --only quotes --quotes-limit 200` 做串行写 vs async_write 耗时对比，写入验收报告附录。
2. 阶段 B：将长连接模块改为 fetch-then-`write_session`。
3. 实现 `write_batch_size>1` 的同表 micro-batch upsert（需定义失败回滚粒度）。

## 7. 使用说明

```powershell
cd stock-quant-system
# 推荐：数据更新专用入口
.venv\Scripts\python.exe -m scripts.run_data_update

# 仅行情 + Tushare 价格补缺
.venv\Scripts\python.exe -m scripts.run_data_update --only quotes,tushare_prices

# 关闭异步写（排障）
# 在 config.yaml 设 ingestion.pipeline.async_write: false
```
