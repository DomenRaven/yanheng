# 机器可用性测试结果

失败 3 / 共 22

| ID | 结果 | 说明 |
|----|------|------|
| M-S00 | PASS | Streamlit :8503 |
| M-R01 | PASS | pass |
| M-R02 | PASS | pass |
| M-R03 | PASS | pass |
| M-R04 | PASS | pass |
| M-R05 | PASS | pass |
| M-R06 | PASS | pass |
| M-R07 | PASS | pass |
| M-R08 | PASS | pass |
| M-R09 | PASS | pass |
| M-R10 | PASS | pass |
| M-R11 | PASS | pass |
| M-D01 | **FAIL** | ready: False
  universe_active: 5565
  effective_quote_date: 2026-09-17
  naive_max_quote_date: 2026-09-17
  quote_cover |
| M-D02 | **FAIL** | AssertionError: ['涨跌停价 limit_price 覆盖率 0.0% < 90%（截面日 2026-09-17）。需跑 tushare_market_data（含 limit_price）。', 'daily_basic  |
| M-U01 | PASS | 8 files |
| M-U02 | PASS | syntax ok |
| M-U03 | PASS | ok |
| M-A01 | **FAIL** | 跳过：warehouse 未就绪 |
| M-S01 | PASS | HTTP OK, len=11141 |
| M-S02 | PASS | HTTP OK, len=11141 |
| M-S03 | PASS | HTTP OK, len=11141 |
| M-S04 | PASS | HTTP OK, len=11141 |
