# 登录时可选任务：检查就绪；未就绪且未在灌库时后台 ensure_data_fresh --apply。
$ErrorActionPreference = "Stop"
$Root = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
Set-Location $Root

$logDir = Join-Path $Root "data\logs"
New-Item -ItemType Directory -Force -Path $logDir | Out-Null
$logPath = Join-Path $logDir ("startup_data_{0}.log" -f (Get-Date -Format "yyyyMMdd_HHmmss"))

$py = Join-Path $Root ".venv\Scripts\python.exe"
if (-not (Test-Path $py)) {
    "ERROR: no venv at $py" | Out-File $logPath -Encoding utf8
    exit 1
}

function Log([string]$msg) {
    $line = "$(Get-Date -Format o) $msg"
    Write-Host $line
    $line | Out-File -FilePath $logPath -Append -Encoding utf8
}

Log "check_startup_data"
& $py -m scripts.check_startup_data 2>&1 | ForEach-Object { Log $_ }
$code = $LASTEXITCODE
Log "check_startup_data exit=$code"

if ($code -eq 0) { exit 0 }
if ($code -eq 3) {
    Log "ingestion busy, skip apply"
    exit 0
}

Log "ensure_data_fresh --apply --profile auto"
& $py -m scripts.ensure_data_fresh --apply --profile auto 2>&1 | ForEach-Object { Log $_ }
exit $LASTEXITCODE
