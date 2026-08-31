$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
$runtimeRoot = Join-Path $projectRoot 'var\monitor'
New-Item -ItemType Directory -Path $runtimeRoot -Force | Out-Null
$python = if (Get-Command py -ErrorAction SilentlyContinue) { 'py' } elseif (Get-Command python -ErrorAction SilentlyContinue) { 'python' } else { throw 'Python runtime not found' }
$arguments = @()
if ($python -eq 'py') { $arguments += '-3.14' }
$arguments += @(
    '-m', 'etf_rotation.cli', 'monitor',
    '--quotes', (Join-Path $runtimeRoot 'quotes.json'),
    '--watchlist', (Join-Path $projectRoot 'data\monitor\watchlist.json'),
    '--history', (Join-Path $runtimeRoot 'quotes.jsonl'),
    '--alert-history', (Join-Path $runtimeRoot 'alerts.jsonl'),
    '--metadata', (Join-Path $projectRoot 'data\monitor\etf_metadata.json'),
    '--valuation', (Join-Path $projectRoot 'data\monitor\valuation.json'),
    '--calendar', (Join-Path $projectRoot 'data\monitor\market_calendar.json'),
    '--refresh-interval', '5',
    '--host', '127.0.0.1',
    '--port', '8765'
)
$previousPythonPath = $env:PYTHONPATH
$env:PYTHONPATH = Join-Path $projectRoot 'src'
try {
    $process = Start-Process -FilePath $python -ArgumentList $arguments -WorkingDirectory $projectRoot -WindowStyle Hidden -RedirectStandardOutput (Join-Path $runtimeRoot 'monitor.out.log') -RedirectStandardError (Join-Path $runtimeRoot 'monitor.err.log') -PassThru
    Set-Content -LiteralPath (Join-Path $runtimeRoot 'monitor.pid') -Value $process.Id -Encoding ascii
    $process
} finally {
    $env:PYTHONPATH = $previousPythonPath
}
