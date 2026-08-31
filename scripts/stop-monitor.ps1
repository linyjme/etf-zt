$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
$pidPath = Join-Path $projectRoot 'var\monitor\monitor.pid'
$stopped = $false
if (Test-Path -LiteralPath $pidPath) {
    $savedProcessId = Get-Content -LiteralPath $pidPath -ErrorAction SilentlyContinue
    if ($savedProcessId -match '^[0-9]+$') {
        $savedProcess = Get-CimInstance Win32_Process -Filter "ProcessId=$savedProcessId" -ErrorAction SilentlyContinue
        if ($savedProcess -and $savedProcess.CommandLine -match 'etf_rotation\.cli monitor') {
            Stop-Process -Id ([int]$savedProcessId) -Force -ErrorAction SilentlyContinue
            $stopped = $true
        }
    }
    Remove-Item -LiteralPath $pidPath -Force -ErrorAction SilentlyContinue
}
$listeners = Get-NetTCPConnection -LocalPort 8765 -State Listen -ErrorAction SilentlyContinue
if (-not $listeners) {
    if (-not $stopped) { Write-Output 'Monitor service is not running' }
    exit 0
}
$processIds = $listeners.OwningProcess | Sort-Object -Unique
foreach ($processId in $processIds) {
    $process = Get-CimInstance Win32_Process -Filter "ProcessId=$processId"
    if ($process -and $process.CommandLine -match 'etf_rotation\.cli monitor') {
        $parentProcessId = $process.ParentProcessId
        Stop-Process -Id $processId -Force -ErrorAction SilentlyContinue
        $parent = Get-CimInstance Win32_Process -Filter "ProcessId=$parentProcessId" -ErrorAction SilentlyContinue
        if ($parent -and $parent.CommandLine -match 'etf_rotation\.cli monitor') {
            Stop-Process -Id $parentProcessId -Force -ErrorAction SilentlyContinue
        }
    }
}
