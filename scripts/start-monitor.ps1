$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$python = if (Get-Command py -ErrorAction SilentlyContinue) { 'py' } elseif (Get-Command python -ErrorAction SilentlyContinue) { 'python' } else { throw 'Python runtime not found' }
$arguments = @()
if ($python -eq 'py') { $arguments += '-3.14' }
$arguments += @(
    '-m', 'etf_rotation.cli', 'monitor',
    '--quotes', (Join-Path $root 'data\monitor\quotes.json'),
    '--watchlist', (Join-Path $root 'data\monitor\watchlist.json'),
    '--history', (Join-Path $root 'data\monitor\quotes.jsonl'),
    '--alert-history', (Join-Path $root 'data\monitor\alerts.jsonl'),
    '--metadata', (Join-Path $root 'data\monitor\etf_metadata.json'),
    '--calendar', (Join-Path $root 'data\monitor\market_calendar.json'),
    '--refresh-interval', '5',
    '--host', '127.0.0.1',
    '--port', '8765'
)
$previousPythonPath = $env:PYTHONPATH
$env:PYTHONPATH = Join-Path $root 'src'
try {
    Start-Process -FilePath $python -ArgumentList $arguments -WorkingDirectory $root -WindowStyle Hidden -PassThru
} finally {
    $env:PYTHONPATH = $previousPythonPath
}
