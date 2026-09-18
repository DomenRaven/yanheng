# 投产日课 —— 真实环境自用清单

> **目标**：单用户本机 Streamlit + DuckDB，年底约 1 万 **自行券商下单**；系统只出建议与本机模拟，不接券商 API。  
> **验收对照**：`docs/9.16-散户决策链需求规格.md` Must；`docs/phase5-acceptance-report.md` §7。

---

## 1. 每次打开应用前（**投产硬门槛**）

```powershell
cd stock-quant-system
.venv\Scripts\python.exe -m scripts.check_warehouse_readiness
```

**退出码 0** 才表示可跑扫描/建议/明日待办；非 0 时 Streamlit 会 **投产阻塞**（红条 + 禁用按钮）。

| 检查 | 做法 |
|------|------|
| 虚拟环境 | `.venv\Scripts\activate` |
| 空库 / 假新 / 缺 limit_price | 见 §2；后台：`python -m scripts.ensure_data_fresh --apply` |
| 灌库占用写锁 | 蓝条只读 → `watch_data_update` 或 `--resume` |
| 冠军模型 | 页脚 `model_run_id` = `champion.json` |

**关于「实时」**：A 股日频决策链不做 tick 推送；新鲜度 = **有效全市场截面日**（≥80% 股票有 qfq 线）对齐 **最近交易日**，且 `limit_price` / `daily_basic` 覆盖达标（见 `config.yaml` → `production_readiness`）。

启动：

```powershell
cd stock-quant-system
.venv\Scripts\python.exe -m streamlit run app.py --server.port 8501
```

改 UI 或 Python 后需 **重启 Streamlit 进程**，仅刷新浏览器不够。

---

## 2. 数据更新（收盘后 / 周末）

**完整策略**：`docs/data-maintenance-policy.md`（日更 / 周更表、理论依据、北交所东财跳过 + `tushare_bse_quotes` 轻量步、计划任务）。

```powershell
cd stock-quant-system
$py = ".venv\Scripts\python.exe"

# 推荐：工作日 = 持仓/待办量价 + 全市场涨跌停/市值截面
& $py -m scripts.run_daily_refresh --profile weekday_decision

# 周末：全市场行情 + 财务/行为 + 复权；之后可双池重训 / 双池掘金
& $py -m scripts.run_daily_refresh --profile weekend_research
# & $py -m mlops.retrain_schedule --pools
# & $py -m scripts.run_weekend_dual_scan

& $py -m scripts.run_daily_refresh --profile bootstrap       # 首次空库（overnight）
& $py -m scripts.run_daily_refresh --profile incremental   # 旧日常（仍含 bj 东财，不推荐默认）

# 本机启动自检（不自动灌库）
& $py -m scripts.check_startup_data

# 检查 + 可选后台启动（未就绪且未在跑时；--profile auto 按星期选 preset）
& $py -m scripts.ensure_data_fresh
& $py -m scripts.ensure_data_fresh --apply --profile auto

# 监测进度
& $py -m scripts.watch_data_update --interval 30

# 底层入口（与 daily_refresh 相同引擎）
& $py -m scripts.run_data_update --resume
& $py -m scripts.run_data_update --skip fundamentals,income_statement,corporate_actions

# index_weight 历史缺口
& $py -m scripts.run_daily_refresh --profile index_weight_gap
```

### Windows 计划任务（收盘后自动增量）

**推荐：仓库脚本注册（执行一次）**

```powershell
cd stock-quant-system\scripts
powershell -NoProfile -ExecutionPolicy Bypass -File .\install_windows_maintenance_tasks.ps1
```

详见 `docs/manuals/windows-scheduled-maintenance.md`（含卸载、自定义时间、可选 `-IncludeLogonAutoApply`）。

**手动**（任务计划程序）：

1. 任务计划程序 → 创建任务 → 触发器：工作日 **16:15**（或你收盘后习惯时间）；另可加 **00:30** 补跑。  
2. 操作：程序 `E:\...\stock-quant-system\.venv\Scripts\python.exe`  
   参数：`-m scripts.run_daily_refresh --profile weekday_decision`  
   起始于：`...\stock-quant-system`  
3. 周六任务：`-m scripts.run_daily_refresh --profile weekend_research`  
4. 条件：仅 AC 电源可选；**勿**与已手动打开的 Streamlit 写库并行（会只读）。  
5. 完成后：`check_warehouse_readiness` 或 `check_startup_data` 应为 0。

断点：`data/ingestion_pipeline_state.json`。日志：`data/logs/`。

---

## 3. 工作日决策链（推荐顺序）

```text
收盘后（数据更新完成）
  → 「持仓与建议」：录入/核对手动持仓（实盘镜像，与 paper 分离）
  → 「生成/刷新建议卡片」或「一键练习闭环」（后者 = 建议 + 本机模拟批量成交）
  → 「明日待办」：≤3 条，核对执行日与失效条件
次日
  → 券商按待办 **自行下单**（或仅点「批量模拟成交」练手）
  → 「历史建议复盘」：M15 手动确认成交价（可选）
收盘后
  → 再看持仓类建议（止损/减仓/止盈 + 股数）
周末
  → 可选 `mlops/retrain_schedule.py`（冠军替换须 Walk-Forward + 组合收益门禁）
  → 模拟盘 expander 看近 7 日摘要（非独立周报页）
```

**1 万默认约束**（`config.yaml`）：最多 2–3 只、单票 ≤15% 净值、现金缓冲 ≥10%；买不起 1 手 → `watch`。

---

## 4. 投产前自检（回归脚本）

在 `stock-quant-system` 下：

```powershell
.venv\Scripts\python.exe scripts\test_paper_broker.py
.venv\Scripts\python.exe scripts\test_advice_sizing.py
.venv\Scripts\python.exe scripts\test_entry_rules.py
.venv\Scripts\python.exe scripts\test_paper_batch_simulate.py
.venv\Scripts\python.exe scripts\test_advice_trace.py
.venv\Scripts\python.exe scripts\test_reason_pack.py
.venv\Scripts\python.exe scripts\test_industry_cap.py
.venv\Scripts\python.exe scripts\audit_limit_price_coverage.py
.venv\Scripts\python.exe scripts\demo_retail_chain.py
.venv\Scripts\python.exe scripts\test_champion_only.py
.venv\Scripts\python.exe scripts\test_m8_invalid_if.py
.venv\Scripts\python.exe scripts\audit_phase5_spec_compliance.py
.venv\Scripts\python.exe scripts\probe_retail_chain.py
.venv\Scripts\python.exe -m scripts.check_warehouse_readiness
```

人工走一遍：`docs/manuals/人工可用性测试指南.md`；日常使用：`docs/manuals/用户使用说明书.md`。

---

## 5. 仍未做 / 不做的边界（#7 诚实）

| 类别 | 说明 |
|------|------|
| **Won’t** | 券商自动报单、收益承诺、LLM 直接当交易指令 |
| **Should 未齐** | 独立周报页 S6；S4 置信度仍是分位不是校准概率；S5 新闻不做自动交易 |
| **Must 半套** | 组合优化权重 **不**自动换算成 open 股数；M16 无券商自动对账，靠 `advice_id` 本机 trace |
| **方法论** | 模拟 = 次日开盘 ± 滑点；不是 Level-2、不是实盘收益 |
| **文档** | Word 手册可能滞后于 Streamlit；以 `phase5-acceptance-report` + 本日课为准 |

---

*版本：2026-09-17。与 Phase 5 Must 同步更新。*
