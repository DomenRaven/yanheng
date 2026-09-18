# 面板重建 + 双池重训 实时探针


## 2026-09-18T23:27:19 · preflight

- `[23:27:19] [preflight] OK      warehouse_ready — {'universe_active': 5565, 'effective_quote_date': '2026-09-17', 'naive_max_quote_date': '2026-09-17', 'quote_coverage_ratio': 0.9959, 'expected_latest_trade_date': '2026-09-18', 'quote_lag_trading_days': 1, 'limit_price_coverage': 1.0184, 'daily_basic_coverage': 0.9978, 'trade_calendar_rows': 8797}`
- `[23:27:19] [preflight] OK      quotes_max — 2026-09-17`
- `[23:27:19] [preflight] OK      calendar_max — 2026-12-31`
- `[23:27:19] [preflight] OK      universe — active=5565 bj=344`
- `[23:27:19] [preflight] OK      calendar_2026-08-31 — present`
- `[23:27:19] [preflight] OK      quotes_2026-08-31_coverage — symbols=5545`
- `[23:27:19] [preflight] OK      rebalance_dates_2026YTD — 2026-01-30, 2026-02-27, 2026-03-31, 2026-04-30, 2026-05-29, 2026-06-30, 2026-07-31, 2026-08-31, 2026-09-18`
- `[23:27:19] [preflight] OK      panel_on_disk_before — max=2026-07-31 rows=501703`

合计 ok=8 fail=0 warn=0

## 2026-09-18T23:27:57 · panel

- `[23:27:57] [panel] OK      panel_range — 2016-01-29 ~ 2026-08-31 dates=128 rows=507333`
- `[23:27:57] [panel] OK      panel_last5 — [datetime.date(2026, 4, 30), datetime.date(2026, 5, 29), datetime.date(2026, 6, 30), datetime.date(2026, 7, 31), datetime.date(2026, 8, 31)]`
- `[23:27:57] [panel] OK      panel_covers_aug_month_end — max=2026-08-31`
- `[23:27:57] [panel] OK      panel_exchange_sh — rows=212302 max=2026-08-31`
- `[23:27:57] [panel] OK      panel_exchange_sz — rows=278625 max=2026-08-31`
- `[23:27:57] [panel] OK      panel_exchange_bj — rows=16406 max=2026-08-31`

合计 ok=6 fail=0 warn=0

## 2026-09-18T23:30:10 · champions

- `[23:30:10] [champions] OK      champion_hs — run=20260918_232953 date_range=['2016-01-29', '2026-08-31'] rows=490927 wf=0.0554 promoted=挑战者RankIC=0.0554 >= 冠军RankIC=0.0608 - 容差`
- `[23:30:10] [champions] OK      champion_hs_fresh_vs_aug — end=2026-08-31`
- `[23:30:10] [champions] OK      champion_bj — run=20260918_233000 date_range=['2016-01-29', '2026-08-31'] rows=16406 wf=0.0833 promoted=挑战者RankIC=0.0833 >= 冠军RankIC=0.0802 - 容差`
- `[23:30:10] [champions] OK      champion_bj_fresh_vs_aug — end=2026-08-31`

合计 ok=4 fail=0 warn=0

## 2026-09-18T23:30:26 · functions

- `[23:30:11] [functions] OK      load_champions — hs=20260918_232953 bj=20260918_233000`
- `[23:30:18] [functions] OK      scan_hs — n=5001 asof=2026-09-17 top=20`
- `[23:30:24] [functions] OK      scan_bj — n=341 asof=2026-09-17 top=20`
- `[23:30:24] [functions] OK      persistence_rank — hits=88 sample=688459 hist=3`
- `[23:30:26] [functions] OK      paper_broker_tests — exit 0`

合计 ok=5 fail=0 warn=0
