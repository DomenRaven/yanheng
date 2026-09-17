# 数据更新引擎 — 约束文档

## 1. 架构约束（八荣八耻 #6）

- **分层**：`ingestion/` 仍负责各数据源字段映射与 upsert 语义；`common/ingestion_engine/` 只负责并发与写调度，不嵌入业务 SQL。
- **DuckDB**：全仓库单文件；写入必须经过 `write_session()`；禁止 sidecar 双库回归。
- **配置**：扩展 `config.yaml` 的 `ingestion.pipeline` 段，不硬编码队列深度。

## 2. 数据源约束（八荣八耻 #1、#3）

- AkShare / Tushare 字段与单位换算仍以各 `ingestion/*.py` 为准；引擎不得改 normalize 逻辑。
- Tushare：单进程 `workers=1` + `tushare_limiter()`；不因加速而提高发车间隔违规风险。

## 3. 并发约束

| 角色 | 允许 | 禁止 |
|------|------|------|
| Fetch 工人线程 | HTTP、纯内存 normalize（若放在 fetch_fn 内） | `get_connection()` / `write_session()` |
| 写线程 | `on_result` → `write_session` / upsert | HTTP |
| 编排主线程 | 等待步骤完成、汇总 | 与写线程同时写库（写只在写线程） |

## 4. 错误与稳定性

- 单条 fetch 失败：回调一次 `on_result(item, None, exc)`，写线程写 `sync_log`（由回调负责，与现网一致）。
- 写线程内未捕获异常：记录日志，继续消费队列，最后汇总 `writer_errors`。
- 关闭顺序：fetch 池结束 → 发送写线程 poison pill → `join` 写线程。

## 5. 测试约束（八荣八耻 #5）

- 使用 `STOCK_QUANT_DB` 指向临时 `.duckdb`，不打开正在使用的 `data/warehouse.duckdb`。
- 证据：单元测试 + 可选 micro-benchmark（串行 vs async_write 耗时比）。

## 6. 已知局限（八荣八耻 #7）

- DuckDB 官方支持进程内多写线程，但本项目 Windows 文件锁 + Streamlit 读库场景下，**仍选择应用层单写线程**，与 `_WRITE_LOCK` 双重保守，未做跨进程压测。
- `async_write=true` 时 `on_result` **不再**保证运行在主线程；依赖写线程单消费者即可，UI 不应假设主线程写库。
