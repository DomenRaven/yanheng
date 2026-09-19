# 影子仓农场（Shadow Farm）

> 机器多策略纸面体检；**短窗盈亏不进入 retrain / 冠军晋升**。

## 动机

人手「一万练习账户」适合学规则。监控应交给机器：按少量固定规矩并行记账，再按 1/3/7/15/20/30 日汇总净值，供 UI 查看、导出或交给大模型**翻译已有数字**。监测原始序列落盘 CSV/JSON，方便离线复盘。

## 理论索引

- `docs/03-量化方法/14-量化MLOps与模型生命周期.md` §5 影子交易作上线/监控闸门  
- `docs/04-风险管理/11-纸面账户与影子交易.md`：人手纸面与 MLOps 影子分层；模拟≠技能证明  
- `docs/03-量化方法/15-日频信号与次日执行.md`：成交默认次日开盘  
- `docs/03-量化方法/05-回测与评估.md`：过拟合防控——短窗多策略不可当主考官  

## 周队列账户（不是「永远固定 8 户」）

- **策略规矩固定 8 套**（见下表），但**账户按 ISO 周新建**：每周一队列 `cohort_id`（如 `2026-W38`）对应 8 个 `paper_account`。  
- **留存**：账户一经建立至少 **60 天**；期满由日更任务 `prune` 清理现金/持仓/成交（`shadow_nav_daily` 与磁盘归档**保留**）。  
- **同时活跃上限（粗算）**：`8 × floor(60/7) = 64`（`MAX_CONCURRENT_ACCOUNTS`）；ISO 周边界偶发可略高，仍按「创建日 + 60 天」prune。30 天内约 ~40 户。  
- 日更（03:00）：对留存期内**全部**影子账户盯市记账；本周队列默认**不换票**。  
- 周末链路 / UI「立即跑一批」：确保本周 8 户并按最新建议换票。  

## 8 套策略规矩

| strategy_id | 说明 |
|-------------|------|
| `hs_todos` / `bj_todos` | 该池明日待办原文股数 |
| `hs_top5/10/20` | 观察 open 按排名取前 N，等权尽量满仓 |
| `hs_top10_half` | 前 10，目标市值约 50% 净值（已达目标则只盯市） |
| `bj_top5/10` | 北交所池对应 top-N |

初始资金模板：`live_prep_10k`。账户 `paper_account.kind='shadow'` + `cohort_id`，与人手练习隔离。账户 ID 形如 `shadow-{cohortSlug}-{strategy_id}`。

## 监测落盘（复盘用）

每次 `run_shadow_farm_once` 成功盯市后写入（目录已 gitignore）：

| 路径 | 内容 |
|------|------|
| `data/shadow_farm/accounts/<account_id>/nav_daily.csv` | 该户净值日序列 |
| `…/trades.csv` | 成交 |
| `…/positions.csv` | 当前持仓 |
| `…/meta.json` / `latest.json` | 元数据 + horizons |
| `data/shadow_farm/panel_latest.csv` / `.json` | 当日横截面（全活跃户） |
| `data/shadow_farm/panel_YYYY-MM-DD.csv` | 横截面按日归档 |
| `data/shadow_farm/accounts_index.json` | 导出索引 |
| `data/shadow_farm/latest_report.json` | 本轮跑批摘要 |

户从库中 prune 后，**磁盘归档不删**，便于事后整理。

## 调度

- Windows 任务：`StockQuant-ShadowFarm`，默认每天 **03:00**（盯市全部未过期账户；**不** `--open-cohort`）  
- Windows 任务：`StockQuant-WeekendResearch`，默认周六 **09:00** → **周末链路**  
  `weekend_research` 灌库成功 → 自动双池 `generate_daily_advice` → `run_shadow_farm_once(open_new_cohort=True)`  
  入口：`scripts/invoke_scheduled_weekend_shadow_chain.ps1` → `python -m scripts.run_weekend_shadow_chain`  
- 日更影子：**不灌行情**；依赖 00:30 / 16:15 `weekday_decision`。无行情 → `skipped_data`  
- 无 `advice_log` 快照时只盯市、不新开仓（周末链路会自动补建议，避免无人点 UI）  
- 链路入口会清除「空步骤 / 过久」的陈旧 `running` 断点，避免 UI 永久显示更新中；有真实进度的断点不自动清  

## 产出

- 表：`shadow_nav_daily`（PK=`account_id,as_of`）、`shadow_run_log`、`paper_account.cohort_id`  
- 文件：见上「监测落盘」  
- UI：`pages/9_影子仓体检.py`（本周对比 + 往周净值快照；CSV / JSON / Markdown + 可选 AI 解读）  

## 明确不做

- 不按 1～15 日影子盈亏 fine-tune 或自动替换冠军  
- 不开上千盘参数网格（首版固定 8 套**规矩**，账户按周滚动）  

## 工程备注（2026-09-19）

- 纸面幂等按 **账户 + advice_id**（影子仓互不抢占人手练习成交记录）  
- 影子 `advice_id` 形如 `shadow:{strategy_id}:{base}:{as_of}`  
- top_n 半仓等权不足 1 手时回退为按排名贪心建仓  
- `simulate_advice_cards` 不可嵌在外层 `write_session` 内（写锁非可重入）  
- 回归：`python -m scripts.test_shadow_farm`  
