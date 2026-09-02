$ErrorActionPreference = 'Stop'

function ConvertTo-WindowsCommandLineArgument {
    param(
        [AllowNull()]
        [AllowEmptyString()]
        [string]$Argument
    )

    if ($null -eq $Argument) { $Argument = '' }
    if ($Argument.Length -gt 0 -and $Argument -notmatch '[\s"]') {
        return $Argument
    }

    $quoted = '"'
    $backslashCount = 0
    foreach ($character in $Argument.ToCharArray()) {
        if ($character -eq '\') {
            $backslashCount++
            continue
        }
        if ($character -eq '"') {
            $quoted += ('\' * (($backslashCount * 2) + 1))
            $quoted += '"'
            $backslashCount = 0
            continue
        }
        if ($backslashCount -gt 0) {
            $quoted += ('\' * $backslashCount)
            $backslashCount = 0
        }
        $quoted += $character
    }
    if ($backslashCount -gt 0) {
        $quoted += ('\' * ($backslashCount * 2))
    }
    return $quoted + '"'
}

function Write-MonitorPidSafely {
    param(
        [Parameter(Mandatory)]
        [System.Diagnostics.Process]$Process,
        [Parameter(Mandatory)]
        [string]$PidPath,
        [Parameter(Mandatory)]
        [string]$RuntimeRoot,
        [scriptblock]$Writer = {
            param($Destination, $ProcessId)
            Set-Content -LiteralPath $Destination -Value $ProcessId -Encoding ascii
        }
    )

    $writeError = $null
    $safePidPath = $false
    $pidFullPath = $null
    try {
        $rootFullPath = [IO.Path]::GetFullPath($RuntimeRoot).TrimEnd('\', '/')
        $pidFullPath = [IO.Path]::GetFullPath($PidPath)
        $pidParent = [IO.Path]::GetDirectoryName($pidFullPath).TrimEnd('\', '/')
        if (-not [string]::Equals($pidParent, $rootFullPath, [StringComparison]::OrdinalIgnoreCase)) {
            throw "Refusing to write PID outside runtime directory: $pidFullPath"
        }
        $safePidPath = $true
        & $Writer $pidFullPath $Process.Id
        return
    } catch {
        $writeError = $_
    }

    $cleanupErrors = [System.Collections.Generic.List[string]]::new()
    try {
        $Process.Refresh()
        if (-not $Process.HasExited) {
            Stop-Process -Id $Process.Id -Force -ErrorAction Stop
            $Process.WaitForExit()
        }
    } catch {
        $cleanupErrors.Add("process cleanup failed: $($_.Exception.Message)")
    }
    try {
        if ($safePidPath -and (Test-Path -LiteralPath $pidFullPath -PathType Leaf)) {
            Remove-Item -LiteralPath $pidFullPath -Force -ErrorAction Stop
        }
    } catch {
        $cleanupErrors.Add("PID cleanup failed: $($_.Exception.Message)")
    }
    if ($cleanupErrors.Count -gt 0) {
        $writeError.Exception.Data['MonitorPidCleanupErrors'] = ($cleanupErrors -join '; ')
    }
    throw $writeError
}

$projectRoot = Split-Path -Parent $PSScriptRoot
$runtimeRoot = Join-Path $projectRoot 'var\monitor'
$swingRuntimeRoot = Join-Path $projectRoot 'var\swing'
New-Item -ItemType Directory -Path $runtimeRoot -Force | Out-Null
New-Item -ItemType Directory -Path (Join-Path $swingRuntimeRoot 'backtests') -Force | Out-Null
$pythonCommand = $null
$pythonArguments = @()
$probeMarker = "__ETF_ROTATION_MONITOR_PYTHON_312_$([guid]::NewGuid().ToString('N'))__"
$probeCode = "import sys; sys.exit(1) if sys.version_info < (3, 12) else print('$probeMarker')"
$candidates = @(
    [pscustomobject]@{ Command = 'python'; Arguments = @() }
    [pscustomobject]@{ Command = 'python3'; Arguments = @() }
    [pscustomobject]@{ Command = 'py'; Arguments = @('-3') }
)
foreach ($candidate in $candidates) {
    $applications = @(Get-Command $candidate.Command -CommandType Application -All -ErrorAction SilentlyContinue)
    foreach ($application in $applications) {
        $command = $application.Source
        $candidateArguments = $candidate.Arguments
        $probeOutput = @()
        $probeExitCode = -1
        try {
            $global:LASTEXITCODE = -1
            $probeOutput = @(& $command @candidateArguments -c $probeCode 2>$null)
            $probeExitCode = $LASTEXITCODE
        } catch {
            $probeOutput = @()
            $probeExitCode = -1
        }
        if ($probeExitCode -eq 0 -and $probeOutput.Count -eq 1 -and [string]$probeOutput[0] -ceq $probeMarker) {
            $pythonCommand = $command
            $pythonArguments = $candidateArguments
            break
        }
    }
    if ($null -ne $pythonCommand) { break }
}
if ($null -eq $pythonCommand) {
    throw 'Python 3.12 or newer runtime not found'
}

$arguments = @($pythonArguments) + @(
    '-m', 'etf_rotation.cli', 'monitor',
    '--quotes', (Join-Path $runtimeRoot 'quotes.json'),
    '--watchlist', (Join-Path $projectRoot 'data\monitor\watchlist.json'),
    '--history', (Join-Path $runtimeRoot 'quotes.jsonl'),
    '--alert-history', (Join-Path $runtimeRoot 'alerts.jsonl'),
    '--metadata', (Join-Path $projectRoot 'data\monitor\etf_metadata.json'),
    '--valuation', (Join-Path $projectRoot 'data\monitor\valuation.json'),
    '--calendar', (Join-Path $projectRoot 'data\monitor\market_calendar.json'),
    '--swing-watchlist', (Join-Path $projectRoot 'data\swing\watchlist.json'),
    '--swing-strategy', (Join-Path $projectRoot 'data\swing\strategy.json'),
    '--swing-daily-history', (Join-Path $swingRuntimeRoot 'daily_quotes.jsonl'),
    '--swing-portfolio', (Join-Path $swingRuntimeRoot 'portfolio.json'),
    '--swing-trades', (Join-Path $swingRuntimeRoot 'trades.jsonl'),
    '--swing-alerts', (Join-Path $swingRuntimeRoot 'alerts.jsonl'),
    '--swing-backtests', (Join-Path $swingRuntimeRoot 'backtests'),
    '--refresh-interval', '60',
    '--host', '127.0.0.1',
    '--port', '8765'
)
$previousPythonPath = $env:PYTHONPATH
$env:PYTHONPATH = Join-Path $projectRoot 'src'
try {
    $stderrPath = Join-Path $runtimeRoot 'monitor.err.log'
    $quotedArguments = @($arguments | ForEach-Object {
        ConvertTo-WindowsCommandLineArgument -Argument ([string]$_)
    })
    $process = Start-Process -FilePath $pythonCommand -ArgumentList $quotedArguments -WorkingDirectory $projectRoot -WindowStyle Hidden -RedirectStandardOutput (Join-Path $runtimeRoot 'monitor.out.log') -RedirectStandardError $stderrPath -PassThru
    $process.WaitForExit(750) | Out-Null
    if ($process.HasExited) {
        $detail = if (Test-Path -LiteralPath $stderrPath) { (Get-Content -LiteralPath $stderrPath -Raw).Trim() } else { '' }
        throw ("Monitor service failed to start" + $(if ($detail) { ": $detail" } else { '' }))
    }
    Write-MonitorPidSafely -Process $process -PidPath (Join-Path $runtimeRoot 'monitor.pid') -RuntimeRoot $runtimeRoot
    $process
} finally {
    $env:PYTHONPATH = $previousPythonPath
}
