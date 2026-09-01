$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
$runtimeRoot = Join-Path $projectRoot 'var\monitor'
New-Item -ItemType Directory -Path $runtimeRoot -Force | Out-Null
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
    '--refresh-interval', '60',
    '--host', '127.0.0.1',
    '--port', '8765'
)
$previousPythonPath = $env:PYTHONPATH
$env:PYTHONPATH = Join-Path $projectRoot 'src'
try {
    $stderrPath = Join-Path $runtimeRoot 'monitor.err.log'
    $process = Start-Process -FilePath $pythonCommand -ArgumentList $arguments -WorkingDirectory $projectRoot -WindowStyle Hidden -RedirectStandardOutput (Join-Path $runtimeRoot 'monitor.out.log') -RedirectStandardError $stderrPath -PassThru
    $process.WaitForExit(750) | Out-Null
    if ($process.HasExited) {
        $detail = if (Test-Path -LiteralPath $stderrPath) { (Get-Content -LiteralPath $stderrPath -Raw).Trim() } else { '' }
        throw ("Monitor service failed to start" + $(if ($detail) { ": $detail" } else { '' }))
    }
    Set-Content -LiteralPath (Join-Path $runtimeRoot 'monitor.pid') -Value $process.Id -Encoding ascii
    $process
} finally {
    $env:PYTHONPATH = $previousPythonPath
}
