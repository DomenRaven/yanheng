# 周末无人值守：weekend_research → 双池建议 → 影子仓农场
# 使用 pyvenv home 解释器 + venv site-packages，避免 Windows venv 双进程抢 DuckDB。
param()

$ErrorActionPreference = "Stop"
$Root = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
Set-Location $Root

$logDir = Join-Path $Root "data\logs"
New-Item -ItemType Directory -Force -Path $logDir | Out-Null
$stamp = Get-Date -Format "yyyyMMdd_HHmmss"
$logPath = Join-Path $logDir "scheduled_weekend_shadow_chain_${stamp}.log"

$venvDir = Join-Path $Root ".venv"
$cfg = Join-Path $venvDir "pyvenv.cfg"
$site = Join-Path $venvDir "Lib\site-packages"
if (-not (Test-Path $cfg)) { Write-Error "缺少 $cfg" }
$homeLine = Get-Content $cfg | Where-Object { $_ -match '^\s*home\s*=' } | Select-Object -First 1
$pyHome = ($homeLine -split '=', 2)[1].Trim()
$py = Join-Path $pyHome "python.exe"
if (-not (Test-Path $py)) { Write-Error "未找到解释器: $py" }

$env:VIRTUAL_ENV = $venvDir
$env:PYTHONPATH = $site
$env:PYTHONNOUSERSITE = "1"

"=== $(Get-Date -Format o) weekend_shadow_chain py=$py ===" | Out-File -FilePath $logPath -Encoding utf8
& $py -u -m scripts.run_weekend_shadow_chain *>> $logPath
$exit = $LASTEXITCODE
"=== exit=$exit $(Get-Date -Format o) ===" | Out-File -FilePath $logPath -Append -Encoding utf8
exit $exit
