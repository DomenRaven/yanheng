# Phase 5 约束文档

> 优先级：道德底线 > 完成速度。本文件约束 **模拟盘与可执行建议** 的实现；数据更新引擎约束仍见 `ingestion-engine-constraints.md`。

## 1. 架构（八荣八耻 #4 #6）

- **分层**：`research/a_share_rules.py` 纯函数；`panel.py` 仍是 research 里唯一摸库模块。`paper_broker` 可以摸库记账，但 **涨跌幅/费用不得在 broker 内重写一套**。  
- **DuckDB 单写者**：写入走 `write_session()`；开发用 `STOCK_QUANT_DB` 临时文件。  
- **禁止 sidecar 双库**。  
- **scanner 只加载** `mlops/registry/champion.json`。  
- **UI 禁止复制** 仓位公式；页面只调 `advice/` `risk/`。  
- `paper_*` 与 `positions` **分表**；不得把模拟成交 upsert 进手动实盘持仓。

## 2. A 股规则（#1 #3）

- 手数、T+1、涨跌停、停牌、费用：以 `a_share_rules.py` + `limit_price` / `suspend_calendar` / 当日行情为准。  
- 调用第三方或表字段前查 ingestion / Tushare 文档或小样本，禁止猜列名。  
- 印花税、过户费、最低佣金与 `../../docs/01-基础概念/09-A股费用与可成交性.md` 及 `CostAssumptions` 对齐；法定税率变了只改假设数据类 + 理论核验日期。  
- ST 涨跌幅必须走 `ST_LIMIT_CHANGE_DATE`（2026-07-06），禁止写回 5%。

## 3. 产品 Won’t（不可「做着方便」打开）

- 自动券商下单、条件单到柜台、QMT 报单。  
- 收益承诺、把模拟净值当实盘宣传。  
- 秒级/高频。  
- LLM 直接输出买卖指令。  
- 因 `pnl<0` 生成 `add`。  
- 为让择时「好看」改 Walk-forward 口径。

## 4. 并发与数据更新

- 全量 `run_data_update` 占用 `warehouse.duckdb` 时：**不** DDL 主库、**不** 杀写进程、**不** 为查进度开写连接。只读监测用 `watch_data_update` 或日志 Tail。  
- Streamlit 与写库冲突时显示中文提示，不 traceback。

## 5. 模拟盘真实性下限

必须拒绝：碎股买入、当日买入当日卖、涨停买入、跌停卖出、现金不足、重复 `advice_id` 成交。  
滑点参数可配，UI 必须写「非真实盘口」。

## 6. 测试（#5）

- 临时库；跑完删一次性探测脚本的 `__pycache__`。  
- 「能跑不报错」不算过。  
- 1 万账户买不起 1 手必须有断言。

## 7. 文档与秘密（#7）

- 不提交 `.env`、`*.duckdb`、论文 PDF（`docs/99-参考文献/papers/*.pdf`）。  
- 不确定的成交口径写进验收「局限」，不装作已用 Level-2 验证。

## 8. 阶段纪律（#8）

一次只做一个阶段（A/B/C/D）。需要改 Must 口径时先改 `9.16-散户决策链需求规格.md` §8 并写理由。
