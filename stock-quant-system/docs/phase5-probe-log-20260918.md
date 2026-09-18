# Phase 5 全流程探针日志（2026-09-18）

> 对照 `9.16-散户决策链需求规格.md` §5 / §7。生产灌库保持暂停。

- 结束时间：2026-09-18T20:03:32
- OK=28  FAIL=0  DATA_BLOCKED/SKIP=2

## 闸门 1.5 测试理论

- 证据包：CD4ML / 本仓库八闸门第 4 步——「能跑不报错」不算过；Must 用临时库数字 + 源码事实。
- 日频执行缺口：`docs/03-量化方法/15` + Perold IS 简化（`04-风险管理/11`），trace 用 fill vs 信号收盘。
- 生产库未就绪记 **DATA_BLOCKED**，不伪装成代码未实现。

## 实时探针输出

```
[20:03:24] OK           M1 champion.json — 20260826_123439
[20:03:24] OK           M1 scanner 只读冠军
[20:03:24] OK           M8 引擎无空话 token
[20:03:24] OK           M18 引擎不赋 add
[20:03:24] OK           M4 UI T+1 说明
[20:03:24] OK           S4 置信度 caption
[20:03:24] OK           模块存在 M16 — advice/advice_trace.py
[20:03:24] OK           模块存在 S1 — advice/reason_pack.py
[20:03:24] OK           模块存在 S2 — advice/industry_cap.py
[20:03:24] OK           模块存在 M5 — advice/position_sizing.py
[20:03:24] OK           模块存在 M11 — advice/entry_rules.py
[20:03:24] OK           模块存在 M9 — advice/paper_broker.py
[20:03:24] OK           compile pages/
[20:03:24] OK           compile app.py
[20:03:24] OK           compile common/ui_theme.py
[20:03:24] OK           compile advice/advice_engine.py
[20:03:24] OK           M16 生产成交条数 — paper_trades=0，完整 trace=0（用户尚未模拟/M15；非字段缺失）
[20:03:24] DATA_BLOCKED 生产库未就绪 — ready=False advice_log=37 paper_trades=0 trace_rows=37 complete_traces=0 {'universe_active': 5565, 'effective_quote_date': '2026-09-17', 'expected_latest_trade_date': '2026-09-18', 'quote_coverage_ratio': 0.9348, 'limit_price_coverage': 0.0, 'daily_basic_coverage': 0.0}; blockers=涨跌停价 limit_price 覆盖率 0.0% < 90%（截面日 2026-09-17）。需跑 tushare_market_data（含 limit_price）。; daily_basic 覆盖率 0.0% < 80%（扫描因子依赖）。
[20:03:24] SKIP         生产 generate_daily_advice / scanner — 灌库暂停且未就绪，禁止写生产库
[20:03:25] OK           scripts/test_paper_broker.py — test_paper_broker: all passed
[20:03:26] OK           scripts/test_advice_sizing.py — test_advice_sizing: all passed
[20:03:26] OK           scripts/test_entry_rules.py — test_entry_rules: passed
[20:03:27] OK           scripts/test_paper_batch_simulate.py — test_paper_batch_simulate: passed
[20:03:28] OK           scripts/test_advice_trace.py — test_advice_trace: passed
[20:03:28] OK           scripts/test_reason_pack.py — test_reason_pack: passed
[20:03:29] OK           scripts/test_industry_cap.py — test_industry_cap: passed
[20:03:30] OK           scripts/test_m8_invalid_if.py — test_m8_invalid_if: passed
[20:03:30] OK           scripts/test_champion_only.py — test_champion_only: passed
[20:03:31] OK           scripts/demo_retail_chain.py — demo_retail_chain: passed
[20:03:32] OK           scripts/audit_phase5_spec_compliance.py — DATA_BLOCKED: 生产库未达投产门槛（规格 §12.4；不冒充 Must 代码失败）
```

## 探明的错误 / 缺漏 / 浅实现

| 级别 | 项 | 细节 |
|------|----|------|
| DATA_BLOCKED | 生产库未就绪 | ready=False advice_log=37 paper_trades=0 trace_rows=37 complete_traces=0 {'universe_active': 5565, 'effective_quote_date': '2026-09-17', 'expected_latest_trade_date': '2026-09-18', 'quote_coverage_ratio': 0.9348, 'limit_price_coverage': 0.0, 'daily_basic_coverage': 0.0}; blockers=涨跌停价 limit_price 覆盖率 0.0% < 90%（截面日 2026-09-17）。需跑 tushare_market_data（含 limit_price）。; daily_basic 覆盖率 0.0% < 80%（扫描因子依赖）。 |

## 浏览器走查（Streamlit :8501）

| 页 | 实况 |
|----|------|
| 首页 | 投产阻塞红条、冠军 `20260826_123439`、决策速览隐藏、页脚数据截止 2026-09-17 |
| 持仓与建议 | 生成/练习按钮 **disabled**；模拟盘 expander 可见 **T+1** 文案 |
| 明日待办 | `st.stop` 于投产阻塞（符合门禁） |
| 历史建议复盘 | 建议 37 / 全链路 trace **0**；诚实说明「不是字段缺失」 |
| 旧建议快照 | 2026-08-24 落盘仍含「仓位大小自己把控」——**当时快照**，新引擎已无此句，不回写历史 |

机器可用性：回归 11/11 + UI 语法 + Streamlit HTTP 4/4 通过；`M-D01/D02/M-A01` 因仓库未就绪失败（与 DATA_BLOCKED 同因，未伪装成代码通过）。

*由 `scripts/probe_retail_chain.py` 生成后追加浏览器走查。*
