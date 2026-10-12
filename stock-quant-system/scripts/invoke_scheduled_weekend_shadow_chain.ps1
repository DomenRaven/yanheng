# 周末无人值守：weekend_research → 双池建议 → 影子仓农场
# 使用 pyvenv home 解释器 + venv site-packages，避免 Windows venv 双进程抢 DuckDB。
param()

$ErrorActionPreference = "Stop"
$Root = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
Set-Location $Root
. (Join-Path $PSScriptRoot "yanheng_scheduled_python.ps1")

$logDir = Join-Path $Root "data\logs"
New-Item -ItemType Directory -Force -Path $logDir | Out-Null
$stamp = Get-Date -Format "yyyyMMdd_HHmmss"
$logPath = Join-Path $logDir "scheduled_weekend_shadow_chain_${stamp}.log"

"=== $(Get-Date -Format o) weekend_shadow_chain ===" | Out-File -FilePath $logPath -Encoding utf8
$exit = Invoke-YanHengPythonLogged -Root $Root -LogPath $logPath -PythonArgs @(
    "-u", "-m", "scripts.run_weekend_shadow_chain"
)
"=== exit=$exit $(Get-Date -Format o) ===" | Out-File -FilePath $logPath -Append -Encoding utf8
exit $exit
