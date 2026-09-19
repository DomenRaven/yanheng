# 供 Windows 计划任务调用：跑影子仓农场并写日志。
# 注意：Windows 下 .venv\Scripts\python.exe 可能是“父进程桩 + 子解释器”双进程，
# 长事务写 DuckDB 时会自锁。这里用 pyvenv.cfg 的 home 解释器 + venv site-packages。
param(
    [string] $AsOf = ""
)

$ErrorActionPreference = "Stop"
$Root = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
Set-Location $Root

$logDir = Join-Path $Root "data\logs"
New-Item -ItemType Directory -Force -Path $logDir | Out-Null
$stamp = Get-Date -Format "yyyyMMdd_HHmmss"
$logPath = Join-Path $logDir "scheduled_shadow_farm_${stamp}.log"

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

"=== $(Get-Date -Format o) shadow_farm py=$py ===" | Out-File -FilePath $logPath -Encoding utf8
$argList = @("-u", "-m", "scripts.run_shadow_farm")
if ($AsOf) {
    $argList += @("--as-of", $AsOf)
}
& $py @argList *>> $logPath
$exit = $LASTEXITCODE
"=== exit=$exit $(Get-Date -Format o) ===" | Out-File -FilePath $logPath -Append -Encoding utf8
exit $exit
