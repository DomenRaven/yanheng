# 沪深打分模型切换探针 2026-09-22

失败 0，通过 17。

```text
[15:15:14] OK     resolve champion — 20260918_232953
[15:15:27] OK     scan hs champion — asof=2026-09-18 n=5012 top10=['603699', '300970', '603900', '300750', '601091', '001277', '688177', '002949', '600697', '002999']
[15:15:27] OK     resolve regress — 20260922_150545_reg
[15:15:27] OK     bj stays champion — 20260918_233000
[15:15:39] OK     hs top10 differ — overlap=0/10 regress_top10=['603991', '301390', '301297', '600391', '001368', '688292', '002988', '002912', '300727', '600880']
[15:15:39] OK     reason text — 回归树扫描未把排序说成冠军模型
[15:15:52] OK     scan bj — asof=2026-09-18 n=341 top5=['920014', '920519', '920665', '920866', '920553']
[15:16:09] OK     advice regress — watch=30 sized=30 bad_lots=0 as_of=2026-09-18
[15:16:09] OK     advice_log while regress — 20260922_150545_reg
[15:16:26] OK     paper regress todos — account=live_prep_10k-e6a29b750ba1 filled=0 pending=3 rejected=0 reasons={'执行日尚无开盘价（待开盘后再练）': 3}
[15:16:50] OK     paper champion todos — filled=3 pending=0 rejected=0 reasons={'价 11.31': 1, '价 6.29': 1, '价 6.83': 1}
[15:16:50] OK     advice champion restored — watch=30 as_of=2026-09-18
[15:16:50] OK     scan_run hs last — 20260918_232953
[15:16:50] OK     scan_run bj last — 20260918_233000
[15:16:50] OK     advice_log hs — 20260918_232953
[15:16:50] OK     champion files untouched
[15:16:50] OK     reason pack static — 排名第1。因子与行为代理暂无冲突提示；排序来自当前选中的打分模型，不因行为提示而改。
```
