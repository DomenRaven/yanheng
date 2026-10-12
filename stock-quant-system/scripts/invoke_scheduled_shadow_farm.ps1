# 供 Windows 计划任务调用：跑影子仓农场并写日志。
# 注意：Windows 下 .venv\Scripts\python.exe 可能是“父进程桩 + 子解释器”双进程，
# 长事务写 DuckDB 时会自锁。这里用 pyvenv.cfg 的 home 解释器 + venv site-packages。
param(
    [string] $AsOf = ""
)

$ErrorActionPreference = "Stop"
$Root = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
Set-Location $Root

# 先落日志再加载包装函数。加载失败也要留下文件，避免计划任务只有退出码、日志目录是空的。
$logDir = Join-Path $Root "data\logs"
New-Item -ItemType Directory -Force -Path $logDir | Out-Null
$stamp = Get-Date -Format "yyyyMMdd_HHmmss"
$logPath = Join-Path $logDir "scheduled_shadow_farm_${stamp}.log"
"=== $(Get-Date -Format o) shadow_farm ===" | Out-File -FilePath $logPath -Encoding utf8

try {
    . (Join-Path $PSScriptRoot "yanheng_scheduled_python.ps1")
} catch {
    "DOTSOURCE_FAIL $($_.Exception.Message)" | Out-File -FilePath $logPath -Append -Encoding utf8
    exit 1
}

$pyArgs = @("-u", "-m", "scripts.run_shadow_farm")
if ($AsOf) {
    $pyArgs += @("--as-of", $AsOf)
}
$exit = Invoke-YanHengPythonLogged -Root $Root -LogPath $logPath -PythonArgs $pyArgs
"=== exit=$exit $(Get-Date -Format o) ===" | Out-File -FilePath $logPath -Append -Encoding utf8
exit $exit
