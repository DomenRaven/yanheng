# 供 Windows 计划任务调用：跑 run_daily_refresh 并写日志。
param(
    [Parameter(Mandatory = $true)]
    [ValidateSet("weekday_decision", "weekend_research", "bootstrap")]
    [string] $Profile
)

$ErrorActionPreference = "Stop"
$Root = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
Set-Location $Root

$logDir = Join-Path $Root "data\logs"
New-Item -ItemType Directory -Force -Path $logDir | Out-Null
$stamp = Get-Date -Format "yyyyMMdd_HHmmss"
$logPath = Join-Path $logDir "scheduled_${Profile}_${stamp}.log"

$py = Join-Path $Root ".venv\Scripts\python.exe"
if (-not (Test-Path $py)) {
    Write-Error "未找到虚拟环境: $py"
}

"=== $(Get-Date -Format o) profile=$Profile ===" | Out-File -FilePath $logPath -Encoding utf8
& $py -m scripts.run_daily_refresh --profile $Profile *>> $logPath
$exit = $LASTEXITCODE
"=== exit=$exit $(Get-Date -Format o) ===" | Out-File -FilePath $logPath -Append -Encoding utf8
exit $exit
