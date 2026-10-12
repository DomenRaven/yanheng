# Shared by invoke_scheduled_*.ps1.
# Windows PowerShell treats native stderr as ErrorRecord. With $ErrorActionPreference=Stop
# that kills the scheduled task on the first Python logging/tqdm line — logs stay at the
# header only (see 2026-09-20 MidnightCatchup). Route through cmd.exe and never Stop on
# native stderr. Also use pyvenv home python to avoid venv launcher dual-process DuckDB lock.

function Get-YanHengPythonHome {
    param([Parameter(Mandatory = $true)][string] $Root)

    $venvDir = Join-Path $Root ".venv"
    $cfg = Join-Path $venvDir "pyvenv.cfg"
    if (-not (Test-Path $cfg)) {
        Write-Error "缺少 $cfg"
    }
    $homeLine = Get-Content $cfg | Where-Object { $_ -match '^\s*home\s*=' } | Select-Object -First 1
    if (-not $homeLine) {
        Write-Error "pyvenv.cfg 无 home="
    }
    $pyHome = ($homeLine -split '=', 2)[1].Trim()
    $py = Join-Path $pyHome "python.exe"
    if (-not (Test-Path $py)) {
        Write-Error "未找到解释器: $py"
    }
    return [PSCustomObject]@{
        Exe     = $py
        VenvDir = $venvDir
        Site    = Join-Path $venvDir "Lib\site-packages"
    }
}

function ConvertTo-CmdExeArgument {
    param([Parameter(Mandatory = $true)][string] $Value)
    if ($Value -notmatch '[ \t&<>^|()"]') {
        return $Value
    }
    return '"' + ($Value -replace '"', '\"') + '"'
}

function Invoke-YanHengPythonLogged {
    param(
        [Parameter(Mandatory = $true)][string] $Root,
        [Parameter(Mandatory = $true)][string] $LogPath,
        [Parameter(Mandatory = $true)][string[]] $PythonArgs
    )

    $info = Get-YanHengPythonHome -Root $Root
    $env:VIRTUAL_ENV = $info.VenvDir
    $env:PYTHONPATH = $info.Site
    $env:PYTHONNOUSERSITE = "1"

    $quoted = @(ConvertTo-CmdExeArgument $info.Exe)
    foreach ($a in $PythonArgs) {
        $quoted += ConvertTo-CmdExeArgument $a
    }
    $cmdLine = ($quoted -join ' ') + ' >> "' + $LogPath + '" 2>&1'
    "py=$($info.Exe)" | Out-File -FilePath $LogPath -Append -Encoding utf8

    $oldEap = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    $oldNative = $null
    if (Get-Variable -Name PSNativeCommandUseErrorActionPreference -ErrorAction SilentlyContinue) {
        $oldNative = $PSNativeCommandUseErrorActionPreference
        Set-Variable -Name PSNativeCommandUseErrorActionPreference -Value $false -Scope Global
    }
    try {
        cmd.exe /c $cmdLine
        if ($null -eq $LASTEXITCODE) { return 1 }
        return [int]$LASTEXITCODE
    }
    finally {
        $ErrorActionPreference = $oldEap
        if ($null -ne $oldNative) {
            Set-Variable -Name PSNativeCommandUseErrorActionPreference -Value $oldNative -Scope Global
        }
    }
}
