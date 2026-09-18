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
| `StockQuant-WeekendResearch` | 周六 09:00 | `weekend_research` |

自定义时间（示例）：

```powershell
powershell -File .\scripts\install_windows_maintenance_tasks.ps1 -WeekdayTime "17:00" -MidnightTime "01:00"
```

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
| `scripts/invoke_scheduled_refresh.ps1` | 任务入口 → `run_daily_refresh` |
| `scripts/invoke_startup_data_maintenance.ps1` | 登录任务入口 |

策略说明见 `docs/data-maintenance-policy.md`。
