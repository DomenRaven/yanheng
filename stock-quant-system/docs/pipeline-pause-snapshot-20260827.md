# 管道暂停快照（2026-08-27）

查询时间：2026-08-27 14:15（主库已能打开）。

过夜 `python -m scripts.daily_pipeline`（2026-08-26 20:32:05 起，PID 87364）已不在运行。
停在 **Tushare `sync_adj_factor`**，最后成功写入的是 `300937`（14:14:04）。
增量已在 DuckDB 里；**不要**恢复 sidecar 双库。这次跑的是暂停前的旧串行代码，HTTP 线程池还没生效。

Streamlit（8501）没动。

## 续跑

下次直接：

```powershell
cd "e:\妙妙工具\炒股辅助\stock-quant-system"
.venv\Scripts\python.exe -m scripts.daily_pipeline --only tushare_prices,tushare_market_data,tushare_behavior
```

`adj_factor` 会按「该股票最大日期」跳过已写到 2026-08-27 的代码。整段全量重跑会再把财务比率从头 HTTP 一遍（本轮已花约 5.4 小时），没有必要。

若只想把今天的日线缺口补上（北交所等失败项），另开：

```powershell
.venv\Scripts\python.exe -m scripts.daily_pipeline --only quotes
```

## 本轮实际走完了哪些步（sync_log，`run_at >= 2026-08-26 20:32`）

| 时间 | 步骤 | 结果 |
|---|---|---|
| 20:32 | universe | 成功 1 次 |
| 20:33 → 08-27 07:18 | quotes | 成功 4965，失败 339 |
| 07:18 → 12:40 | fundamentals（新浪） | 成功 5212 |
| 12:40 → 12:49 | fundamentals 北交所 Tushare | 成功 339 |
| 12:54 | reference_data（披露日历/退市池/行业） | 成功 |
| 12:54 | market_data（交易日历/指数/成分） | 成功 |
| 12:54 → 14:14 **中断** | **adj_factor** | 成功 **2371** 只，最后 `300937` |

本轮 **没有** `sync_income_statement` / `sync_corporate_actions` 逐股日志。北交所财务结束到披露日历之间只有约 5 分钟，不像全市场利润表或公司行为。利润表表内历史覆盖已经是 5549/5550（报告期到 2026-06-30，季报口径正常），但不能据此说这一步刚才跑过。

## 表状态（暂停后查询）

| 表 | 最新日期 | 备注 |
|---|---|---|
| daily_quotes qfq | 2026-08-26 | 该日 5208/5550 只有；失败主要在 quotes 后段 |
| adj_factor | **2026-08-27** | 已有 2368 只有到今天；目标含退市约 5911 只，大约还剩六成 |
| daily_basic | 2026-08-25 | `tushare_market_data` 未跑 |
| limit_price | 2026-08-25 | 同上 |
| index_quotes | 2026-08-26 | |
| suspend_calendar | 2026-08-26 | |
| fundamentals | 2026-06-30 | 5550 只有；季报不是日频 |
| income_statement | 2026-06-30 | 5549 只有 |
| share_changes | 2024-10-21 | 事件日，不是抓取日 |
| dividends | 2026-08-26 | |
| disclosure_calendar | 2026-08-27 | |
| index_weight | 2026-07-31 | 月末权重 |
| moneyflow | 2026-08-25 | `tushare_behavior` 未跑 |
| moneyflow_hsgt | 2026-08-25 | |
| prediction_log | 2026-08-24 | 管道不自动扫描；5342 行 |

## 已知局限

- 中断发生在逐股写入之间，当前这只 `300937` 已记 success，下一只未开始或未提交。DuckDB WAL 已能打开，未见打不开库。
- quotes 本轮失败 339 只（代码看到过北交所东财失败），qfq 08-26 覆盖 93.8%。
- 扫描/预测不会因为这次续跑自动更新。
