# 移除 install_windows_maintenance_tasks.ps1 注册的任务。
$ErrorActionPreference = "Stop"
Import-Module ScheduledTasks -ErrorAction Stop

$names = @(
    "StockQuant-WeekdayDecision",
    "StockQuant-MidnightCatchup",
    "StockQuant-WeekendResearch",
    "StockQuant-StartupDataMaintenance"
)

foreach ($n in $names) {
    $t = Get-ScheduledTask -TaskName $n -ErrorAction SilentlyContinue
    if ($t) {
        Unregister-ScheduledTask -TaskName $n -Confirm:$false
        Write-Host "Removed: $n"
    } else {
        Write-Host "Skip (not found): $n"
    }
}
