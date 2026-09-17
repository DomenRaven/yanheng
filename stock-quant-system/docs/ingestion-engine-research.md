# 数据更新引擎 — 行业背景调研（闸门 1）

对照任务：将「线性逐条写库」升级为「并发取数 + 单写者异步落库」，提高 `daily_pipeline` 类任务的吞吐与稳定性。

## 1. 共性约束：分析型本地库的单写者

| 产品 | 多进程写 | 进程内多线程写 | 典型做法 |
|------|----------|----------------|----------|
| **DuckDB**（本项目） | 默认单写者；多进程需应用层互斥或外部服务 | 单进程内可并发 append/非冲突更新，仍建议序列化写路径 | 短连接 + 应用层写锁（见 `common/db.py`） |
| SQLite | 单写者 | WAL 下多读单写 | 连接池 + 写队列 |
| ClickHouse / 数仓 | 批量 insert | 分区并行 load | Bronze 层批量落地 |

本项目已在 ADR（`docs/parallel-http-ingestion.md`）明确：**放弃 sidecar 双库**，采用进程内 HTTP 并发 + `write_session()` + `_WRITE_LOCK`。

## 2. 成熟模式（可复用到本项目）

### 2.1 生产者-消费者 + 有界队列（背压）

- **做法**：N 个 fetch 工人从任务列表取活；结果放入 `queue.Queue(maxsize=K)`；**单独 1 条写线程**从队列取批/取条，持锁打开 DuckDB 写入。
- **收益**：fetch RTT 与 DB 写入重叠；队列满时 fetch 阻塞，避免 `ThreadPoolExecutor` 一次性 `submit` 全市场任务导致内存里堆积大量已完成 `Future`/DataFrame（`parallel-http-ingestion.md` 已知风险）。
- **参考**：[DuckDB 官方 Concurrency 文档](https://duckdb.org/docs/stable/connect/concurrency.html)；开源实现如 mixpanel-data 的 parallel_fetcher（bounded write queue）；[队列化 ingest 实践](https://dev.to/meemeealm/simple-queue-can-save-your-pipeline-duckdb-python-1hn0)。

### 2.2 编排层与工作层分离

- **Airflow / Prefect / Dagster**：DAG 依赖、重试、调度；适合多机与运维团队，对本机个人量化系统 **过重**。
- **本项目取舍**：保留 `scripts/run_data_update.py` 轻量编排（步骤顺序 + 已知并行波），把复杂度放在 **单步内的 fetch/write 管道**，不引入新调度基础设施。

### 2.3 批量写（micro-batch）

- 同一表、相同 upsert 语义下，合并多条 fetch 结果再一次 `write_session` 可降低连接打开次数。
- **首期**：保持「每条 fetch 结果一次 upsert」语义不变，避免改变 `sync_log` 粒度；`write_batch_size` 预留配置，默认 1。

### 2.4 Medallion（Bronze/Silver/Gold）

- 大型团队用 Bronze 存原始 JSON/Parquet，Silver 清洗，Gold 服务分析。
- **本项目**：DuckDB 表已是清洗后 EAV/主键 upsert，**不新增 Bronze 层**，避免双份存储与合并成本（与 Phase 0.5 sidecar 教训一致）。

## 3. 与本项目现状的差距

| 已有能力 | 缺口 |
|----------|------|
| `map_fetch_then_write`：工人只 fetch，`on_result` 写库 | `on_result` 在**调用线程**同步执行；多工人时写仍与下一批 fetch 争用主线程时间；全量任务一次性 submit |
| Tushare `run_*_loop_sync` + `workers=1` | fetch 与 write **完全串行**，无法重叠 |
| `daily_pipeline` 三路并行（新浪/巨潮/Tushare 利润表） | `tushare_prices` / `sync_bse_cdr_qfq` / `index_weight` 等仍在循环内握 `get_connection()`（文档已列） |

## 4. 选型结论（闸门 2 ADR）

- **选**：在 `common/ingestion_engine/` 实现 **有界队列 + 专用写线程** 的 `bounded_map_fetch_then_write`，由 `run_data_update` 统一入口调用；Tushare 与 HTTP 多工人路径默认启用。
- **放弃**：sidecar 双库、多进程 DuckDB 写、引入 Airflow、首版改写全部 ingestion 模块握连接问题。
- **验收口径**：沙箱可测的工程门禁（背压、写线程单消费者、锁行为、相对串行加速），**不是** IC/RankIC（与 `parallel-http-ingestion.md` 一致）。
