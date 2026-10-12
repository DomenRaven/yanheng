# 探针：Tushare 按交易日批量行情（2026-10-12）

状态：**实现已落地，默认关闭**（`config.yaml` → `ingestion.tushare_bulk_quotes.enabled: false`）。
联网对照由 C2 工作流在 2026-10-12 执行；协调者复核了代码与 9 个合成测试（`python -m scripts.test_tushare_bulk_quotes` 全部通过），**没有重跑** 200 只 × 60 日的联网对照。下面的百分比来自该次探针的输出，原始脚本在 `%TEMP%\tsbulk_probe`（会话结束后已清理）。

## 1. 周末链计时（2026-10-10 日志）

| 步骤 | 耗时 | 结果 |
|------|------|------|
| quotes（逐股新浪） | 约 64.5 分钟 | 5,198 成功、1 失败、349 跳过（bj）、25,701 行 |
| corporate_actions / fundamentals / income_statement 并行 | 各 4 小时以上，fundamentals 约 4.7 小时 | fundamentals 跑完发现没有新数据；income_statement 重写约 840 万行 |
| tushare_prices（逐股 adj_factor） | 约 1.3 秒/只，日志在 5,104/5,906 处截断 | 全链路 7 小时 45 分被切断，advice / shadow 没跑到 |

09-26 那次跑完用了 591 分钟。时间大头是逐股 HTTP，不是计算。

## 2. 10-10 写丢失的根因

13:26 开机后，周末链和工作日补课在同一时间戳启动，两边同时写 DuckDB。`WarehouseBusyError` 出现在约 50 次写入上（约 25 只行情 + 25 条财务/公司行动）。写入错误在步骤内部被接住计数，**没有向上标记失败**，所以流水线对外仍报 ok。这是比“锁冲突导致任务退出”更隐蔽的缺陷：丢了数据还显示成功。

## 3. qfq 一致性（抽样，最近 60 个交易日）

- 非事件票：99.989% 的行相对偏差 ≤ 0.1%，基准比≈1。新浪 qfq 在没有公司行动时就是未复权价。唯一离群：920011（bj）2026-09-15。
- 有除权的票：41.8% 的行偏差 > 0.1%。原因是除权日前写入的行停在旧复权基准上；除权后整段重拉过的票（如 001388）与新基准一致，剩余差异是新浪两位小数的舍入。
- 自 2026-07-01 以来的事件累计：约 508 只、超过 100 万行停在旧基准上，约占 qfq 表的 9%。`factor_mom_12_1` 用 252 日滞后，窗口内的基准断裂会造出虚假收益。
- tushare 有、库里没有：bj 196 行（40 只，对应 09-24 之后 bj 停更）、sz 5 行。
- 换手率：`daily_basic.turnover_rate / 100` 与新浪 turnover 在 5,184 只里 99.8% 落在 ±1% 内。
- 量的单位不一致是既有问题：东财来源的行按手、新浪按股。批量写入按新浪口径（手 ×100 = 股）。

门禁（非事件日 99.9% 行内 ≤0.1%）在这次抽样上通过。系统性偏差都能解释：旧基准、盘中写入的半日行、两位小数舍入。

## 4. 实现与取舍

`ingestion/tushare_market_data.py` 增加按日批量：每个交易日 `daily` + `adj_factor` + `daily_basic` 三次调用，覆盖 sh/sz/bj。当日 qfq 以当日 `adj_factor` 为基准（比值=1，即未复权价）；`adj_factor` 相对上一基准日跳变的票，把该日之前的 qfq OHLC 乘以 `adj_prev/adj_today`（`compute_readjust_ratios` / `apply_readjust`）。成交量手→股、成交额千元→元、换手率百分数→小数。

放弃的方案：继续逐股新浪。它和 tushare 在非事件日几乎一致，但 5,000 次 HTTP 是周末链的时间来源，而且不会自己重定基。

已入库的新浪历史不能靠“增量乘一个比值”修好（旧行的基准本身就是错的）。启用后审计步会抽样对照并按票重建失配历史（`rebuild_symbol_qfq_history`，单次最多 800 只，每只 2 次调用）。C2 估计 508 只约 1,000 次调用、40 分钟量级，这是估算，没有实测。

`orchestrator._run_quotes` 在 `enabled: true` 时走批量，异常且 `fallback_to_sina: true` 时回退逐股。默认保持关闭，因为主库上还没有跑过一次完整的批量周末链。第一次开启用手工命令观察：

```powershell
python -m ingestion.tushare_market_data --task bulk_quotes
```

## 5. 已知局限

- 涨停价、停牌仍走原有表；批量行情不替代它们。
- 重定基只乘 OHLC，不动 volume/amount/turnover/pct_change。
- 写入错误被吞掉的问题（第 2 节）这次没有改；批量路径上线前应让写失败把步骤标成失败。
- 协调者没有独立复跑联网对照；若要打开默认开关，先在主库的副本上跑一轮再改 `enabled`。
