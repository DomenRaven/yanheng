# Windows 计划任务 — 日更 / 周更自启动

仓库已提供安装脚本，**在本机执行一次**即可注册（写入当前 Windows 用户任务，一般无需管理员）。

## 安装

```powershell
cd stock-quant-system\scripts
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\install_windows_maintenance_tasks.ps1 -IncludeLogonAutoApply
```

（若带中文的绝对路径 `-File` 报解析错误，请先 `cd` 到 `scripts` 再执行。）

| 任务名 | 默认时间 | 行为 |
|--------|----------|------|
| `StockQuant-WeekdayDecision` | 周一至五 16:15 | `weekday_decision` |
| `StockQuant-MidnightCatchup` | 每天 00:30 | `weekday_decision`（补跑） |
| `StockQuant-WeekendResearch` | 周六 09:00 | **周末链路**：灌库 → 双池建议 → 影子仓 |
| `StockQuant-ShadowFarm` | 每天 03:00 | 影子仓盯市/记账（用最新建议；不灌行情） |

自定义时间（示例）：

```powershell
powershell -File .\install_windows_maintenance_tasks.ps1 -WeekdayTime "17:00" -MidnightTime "01:00" -ShadowFarmTime "03:30"
```

**影子仓说明**：

- 每天 03:00 的 `StockQuant-ShadowFarm` **不**拉取行情，对留存期内全部影子账户盯市记账，汇总 1/3/7/15/20/30 日净值（默认不换票）。  
- **周队列**：每周新建 8 户（ISO `cohort_id`），账户至少留存 **60 天**（约 30 天内累积到 ~40 户）。  
- 周六 `StockQuant-WeekendResearch` 无人值守链路：`weekend_research` 成功后自动生成沪深/北交所建议，再开本周 8 户并换票。  
- 短窗结果**不**进入模型训练/晋升。UI 见侧边栏「影子仓体检」。  
- 日志：`scheduled_shadow_farm_*.log`、`scheduled_weekend_shadow_chain_*.log`。

### 可选：登录时自动补灌

未就绪且当前没有在灌库时，后台执行 `ensure_data_fresh --apply --profile auto`（会占 DuckDB 写锁，与 Streamlit 勿并行写库）：

```powershell
powershell -File .\scripts\install_windows_maintenance_tasks.ps1 -IncludeLogonAutoApply
```

## 日志

`data/logs/scheduled_<profile>_yyyyMMdd_HHmmss.log`  
登录补灌：`data/logs/startup_data_*.log`

## 卸载

```powershell
powershell -File .\scripts\uninstall_windows_maintenance_tasks.ps1
```

## 实现文件

| 文件 | 作用 |
|------|------|
| `scripts/install_windows_maintenance_tasks.ps1` | 注册任务 |
| `scripts/uninstall_windows_maintenance_tasks.ps1` | 删除任务 |
| `scripts/invoke_scheduled_refresh.ps1` | 日更入口 → `run_daily_refresh` |
| `scripts/invoke_scheduled_shadow_farm.ps1` | 每日影子仓 → `run_shadow_farm` |
| `scripts/invoke_scheduled_weekend_shadow_chain.ps1` | 周末链路 → 灌库 + 双池建议 + 影子仓 |
| `scripts/invoke_startup_data_maintenance.ps1` | 登录任务入口 |

策略说明见 `docs/data-maintenance-policy.md`。
