param(
    [switch] $IncludeLogonAutoApply,
    [string] $WeekdayTime = "16:15",
    [string] $MidnightTime = "00:30",
    [string] $WeekendTime = "09:00",
    [string] $ShadowFarmTime = "03:00"
)
# Register StockQuant maintenance tasks for current Windows user.
# Usage: powershell -NoProfile -ExecutionPolicy Bypass -File scripts\install_windows_maintenance_tasks.ps1
# Optional: -IncludeLogonAutoApply ; -ShadowFarmTime "03:00"

$ErrorActionPreference = "Stop"
$Root = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$Runner = Join-Path $PSScriptRoot "invoke_scheduled_refresh.ps1"
$Startup = Join-Path $PSScriptRoot "invoke_startup_data_maintenance.ps1"
$ShadowRunner = Join-Path $PSScriptRoot "invoke_scheduled_shadow_farm.ps1"
$WeekendChainRunner = Join-Path $PSScriptRoot "invoke_scheduled_weekend_shadow_chain.ps1"

if (-not (Test-Path $Runner)) {
    Write-Error "Missing runner: $Runner"
}
if (-not (Test-Path $ShadowRunner)) {
    Write-Error "Missing shadow farm runner: $ShadowRunner"
}
if (-not (Test-Path $WeekendChainRunner)) {
    Write-Error "Missing weekend shadow chain runner: $WeekendChainRunner"
}

Import-Module ScheduledTasks -ErrorAction Stop

function New-StockQuantTask {
    param(
        [string] $TaskName,
        [string] $Description,
        $Trigger,
        [string] $Argument,
        [string] $ScriptPath = $Runner,
        [int] $TimeoutHours = 6
    )
    $action = New-ScheduledTaskAction `
        -Execute "powershell.exe" `
        -Argument "-NoProfile -ExecutionPolicy Bypass -File `"$ScriptPath`" $Argument" `
        -WorkingDirectory $Root
    $settings = New-ScheduledTaskSettingsSet `
        -AllowStartIfOnBatteries `
        -DontStopIfGoingOnBatteries `
        -StartWhenAvailable `
        -MultipleInstances IgnoreNew `
        -ExecutionTimeLimit (New-TimeSpan -Hours $TimeoutHours)
    Register-ScheduledTask `
        -TaskName $TaskName `
        -Action $action `
        -Trigger $Trigger `
        -Settings $settings `
        -Description $Description `
        -Force | Out-Null
    Write-Host "OK: $TaskName"
}

Write-Host "Root: $Root"
Write-Host ""

$wdTrigger = New-ScheduledTaskTrigger -Weekly -DaysOfWeek Monday, Tuesday, Wednesday, Thursday, Friday -At $WeekdayTime
New-StockQuantTask `
    -TaskName "StockQuant-WeekdayDecision" `
    -Description "YanHeng weekday_decision after market close" `
    -Trigger $wdTrigger `
    -Argument "-Profile weekday_decision"

$midTrigger = New-ScheduledTaskTrigger -Daily -At $MidnightTime
New-StockQuantTask `
    -TaskName "StockQuant-MidnightCatchup" `
    -Description "YanHeng weekday_decision midnight catch-up" `
    -Trigger $midTrigger `
    -Argument "-Profile weekday_decision"

$weTrigger = New-ScheduledTaskTrigger -Weekly -DaysOfWeek Saturday -At $WeekendTime
New-StockQuantTask `
    -TaskName "StockQuant-WeekendResearch" `
    -Description "YanHeng weekend_research → dual-pool advice → shadow farm" `
    -Trigger $weTrigger `
    -Argument "" `
    -ScriptPath $WeekendChainRunner `
    -TimeoutHours 14

$sfTrigger = New-ScheduledTaskTrigger -Daily -At $ShadowFarmTime
New-StockQuantTask `
    -TaskName "StockQuant-ShadowFarm" `
    -Description "YanHeng daily shadow farm MTM (uses latest advice; no model promotion)" `
    -Trigger $sfTrigger `
    -Argument "" `
    -ScriptPath $ShadowRunner

if ($IncludeLogonAutoApply) {
    if (-not (Test-Path $Startup)) {
        Write-Error "Missing startup script: $Startup"
    }
    $logonAction = New-ScheduledTaskAction `
        -Execute "powershell.exe" `
        -Argument "-NoProfile -ExecutionPolicy Bypass -File `"$Startup`"" `
        -WorkingDirectory $Root
    $logonSettings = New-ScheduledTaskSettingsSet `
        -AllowStartIfOnBatteries `
        -DontStopIfGoingOnBatteries `
        -StartWhenAvailable `
        -MultipleInstances IgnoreNew `
        -ExecutionTimeLimit (New-TimeSpan -Hours 1)
    $logonTrigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
    Register-ScheduledTask `
        -TaskName "StockQuant-StartupDataMaintenance" `
        -Action $logonAction `
        -Trigger $logonTrigger `
        -Settings $logonSettings `
        -Description "YanHeng login check and ensure_data_fresh if needed" `
        -Force | Out-Null
    Write-Host "OK: StockQuant-StartupDataMaintenance"
} else {
    Write-Host "Skipped logon task (use -IncludeLogonAutoApply to enable)"
}

Write-Host ""
Write-Host "View: taskschd.msc"
Write-Host "Remove: scripts\uninstall_windows_maintenance_tasks.ps1"
Write-Host "Logs: $Root\data\logs\scheduled_*.log"
