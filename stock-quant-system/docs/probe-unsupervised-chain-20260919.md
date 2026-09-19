# 无人值守链路探针 2026-09-19

> 真实库证据；短窗影子盈亏不进入晋升。

- OK=32 FAIL=0 WARN=0 SKIP=0

## 运行日志

```
[13:11:59] OK           探针启动 — root=E:\妙妙工具\炒股辅助\stock-quant-system
[13:11:59] OK           链路脚本存在 — run_weekend_shadow_chain.py + invoke_scheduled_weekend_shadow_chain.ps1
[13:11:59] OK           weekend_research profile — quotes,fundamentals,income_statement,corporate_actions,…
[13:11:59] OK           champion_hs — 20260918_232953
[13:11:59] OK           champion_bj — 20260918_233000
[13:11:59] OK           UI 文件可读 app.py
[13:11:59] OK           UI 文件可读 pages/1_持仓与建议.py
[13:11:59] OK           UI 文件可读 pages/2_掘金扫描.py
[13:11:59] OK           UI 文件可读 pages/8_明日待办.py
[13:11:59] OK           影子仓页关键文案
[13:11:59] OK           UI 文件可读 pages/9_影子仓体检.py
[13:11:59] OK           UI 文件可读 pages/7_历史建议复盘.py
[13:12:01] OK           StockQuant-ShadowFarm — Ready
[13:12:01] OK           StockQuant-WeekendResearch — Ready → chain
[13:12:01] OK           _ingest_ok 单测逻辑
[13:12:01] OK           _clear_stale_pipeline_lock 可调用
[13:12:01] OK           无进行中灌库锁
[13:12:01] OK           仓库投产就绪 — ready=True blockers=[] eff=2026-09-17 exp=2026-09-18
[13:12:01] OK           advice_log[hs] — as_of=2026-09-17 n=30
[13:12:01] OK           advice_log[bj] — as_of=2026-09-17 n=30
[13:12:01] OK           shadow 表 — nav_rows=9 run_rows=15
[13:12:01] OK           影子账户数 — 8
[13:12:13] OK           回归 scripts/test_shadow_farm.py — ALL PASSED
[13:12:15] OK           回归 scripts/test_paper_broker.py — test_paper_broker: all passed
[13:12:16] OK           回归 scripts/test_champion_only.py — test_champion_only: passed
[13:12:46] OK           链路建议 hs — as_of=2026-09-17 todos=3 watch=30
[13:12:46] OK           链路建议 bj — as_of=2026-09-17 todos=3 watch=30
[13:12:46] OK           链路影子仓 — status=ok as_of=2026-09-18 n=8
[13:12:46] OK           latest_weekend_chain.json — age_s=0
[13:12:46] OK           latest_report.json — status=ok n=8 as_of=2026-09-18
[13:12:46] OK           horizon ret_20d
[13:12:46] OK           horizon ret_30d
```

## 待修复清单

_无 FAIL/WARN/DATA_BLOCKED。_

## 本轮已修（探针过程中发现）

| 问题 | 处理 |
|------|------|
| 探针把 scanner 日志里的 `{` 当成链路 JSON | 链路写入 `data/shadow_farm/latest_weekend_chain.json` + `===WEEKEND_SHADOW_CHAIN_REPORT===` 标记 |
| `hs_top10_half` 对剩余现金再 ×50% 导致买不起 1 手 | `deploy_pct` 改为目标市值/净值；测例在已达目标时跳过 |
| 影子仓 UI 宽表把 `ret_20d`/`ret_30d` 挤出视口 | 基础表与收益表拆开 |
| 今早周末任务空 `running` 断点 | 链路入口清陈旧空/过久断点 |

## 刻意未跑

- 完整 `weekend_research` 全市场灌库（耗时长）；能力由 profile、任务注册与 `--skip-ingest` 链路实测覆盖。下次周六 09:00 由计划任务自动执行。
