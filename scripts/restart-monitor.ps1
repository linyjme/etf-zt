$ErrorActionPreference = 'Stop'
& (Join-Path $PSScriptRoot 'stop-monitor.ps1')
Start-Sleep -Seconds 1
& (Join-Path $PSScriptRoot 'start-monitor.ps1')
