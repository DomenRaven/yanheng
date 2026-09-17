# HTTP 并发 + DuckDB 单写者（2026-08-27）

对照：日常增量入口 `scripts/daily_pipeline.py` 的耗时主要在逐股 HTTP，不是 CPU。
不是新 Phase。验收口径是工程门禁（限速仍全局、写入仍单连接、回调不在工人线程），
不是 IC/RankIC。

## ADR（为什么选这个、放弃了什么）

- **选**：进程内 `ThreadPoolExecutor` 打 HTTP + `IntervalLimiter` 全局发车间隔 +
  `write_session()` 短连接外加进程内 `_WRITE_LOCK`。网络 RTT 远大于 0.35s 间隔时，
  多工人可以同时 in-flight，发车仍不比串行更密。
- **放弃 sidecar 双库**：Phase 0.5 为绕开单写者搭过旁路库，合并回主库成本高，已废弃。
  Windows 上 DuckDB 文件锁是进程级的，再开一个 `.duckdb` 解决不了「Streamlit 要读主库」。
- **放弃 Tushare 多工人**：单步内固定 `workers=1`，限速走 `tushare_limiter()`
  （约 0.35–0.45s，2000 积分档留余量）。对本机当前 tushare 版本「非线程安全」偏保守，见自检。
- **暂不**让 `quotes` 与 `reference_data` / `market_data` / 部分 `tushare_*` 重叠：
  后者仍在 HTTP 循环里握着 `get_connection()`，一旦重叠会把仓库文件锁死。
  要重叠，先把那些模块改成 fetch-then-`write_session`。

## 改了什么

| 模块 | 行为 |
|---|---|
| `common/http_retry.py` | `IntervalLimiter`；`polite_sleep()` 走默认限速器 |
| `common/parallel_fetch.py` | `map_fetch_then_write`：工人只 fetch；`async_write=true` 时 `on_result` 在专用写线程（见 `ingestion-engine-requirements.md`） |
| `common/ingestion_engine/` | 有界队列 + 写线程；`scripts/run_data_update.py` 入口 |
| `common/db.py` | `write_session` 整段持 `_WRITE_LOCK` |
| `ingestion/quotes_batch.py` 等 | 已是最新的股票不提交到池子；Tushare 路径 `workers=1` |
| `scripts/daily_pipeline.py` | quotes 之后并行跑 fundamentals / income / corporate_actions |

配置：`ingestion.http_workers`（默认 6）、`ingestion.rate_limit`、
`ingestion.tushare_rate_limit`。

## 测试（闸门 4）

不打正在跑的主库（夜间 pipeline 可能仍占着 `warehouse.duckdb`）。

```powershell
cd stock-quant-system
.venv\Scripts\python.exe -m scripts.test_parallel_fetch
.venv\Scripts\python.exe -m scripts.test_duckdb_lock
```

- 4 线程共用限速器：相邻发车间隔不应成批撞车
- `on_result` 在调用线程（`workers=4` 与 `workers=1`）
- fetch 异常只回调一次
- 人工延迟 0.12s × 8 任务 / 4 工人，应明显快于串行约 1.12s
- 管道三路并行波：`corporate_actions` 虽不与 fundamentals 相邻，也必须同时开工（Barrier）
- `write_session` 多线程插入不报错、行数正确；跨进程锁回归仍过

未做：对着被占用的主库跑 `--limit` 实盘微基准（会和正在跑的 pipeline 抢锁）。
新代码在**下一次** `python -m scripts.daily_pipeline` 才生效。

## 自检（2026-08-27 10:13 对照代码+源码，不是凭记忆）

- 过夜 `daily_pipeline`（PID 87364，2026-08-26 20:32 起）**仍在跑旧代码**，主库被占。
  本轮没有对着 `warehouse.duckdb` 做 `--limit` 实盘微基准。
- 本机 tushare `DataApi.query` 是每次独立 `requests.post`，没有 Session。
  ADR「客户端非线程安全」对当前安装版本偏保守。`income_statement` 与北交所
  `fina_indicator` 在三路并行后期可能两路同时打 Tushare，但共用 `tushare_limiter`，
  发车间隔仍受控；未对服务端做双连接压测。
- `sync_bse_cdr_qfq_quotes`、`sync_index_weight` 仍在 HTTP 循环里握着
  `get_connection()`（在并行波之后串行跑，会把 Streamlit 锁一段时间）。
- `map_fetch_then_write` 会一次性 `submit` 全部任务；写入慢时已完成的 DataFrame
  会在 Future 里堆积。增量日更通常网络更慢，风险低；全量首跑更要注意。
- 新浪日线 / 财务指标：查过 akshare，每次 `requests.get` 或函数内新建 Session；
  巨潮股本变动每次新建 MiniRacer。未做全市场压测。
- `tushare_prices` / `tushare_market_data` / `tushare_behavior` 以及日历模块仍可能
  在 HTTP 期间握连接；全市场 `daily_basic` 仍是大量 Tushare 串行调用。
- 三路并行时新浪与巨潮共用 `default_limiter()`，发车更保守，不会两路各自 0.35s 叠加速度。
- 限速是「发车间隔」不是「同时 in-flight 上限」；工人数只限制同时挂起的 HTTP 数。
