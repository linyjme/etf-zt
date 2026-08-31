$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
& (Join-Path $PSScriptRoot 'stop-monitor.ps1')
Start-Sleep -Seconds 1
& (Join-Path $projectRoot 'scripts\start-monitor.ps1')
