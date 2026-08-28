$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
$env:PYTHONPATH = Join-Path $projectRoot 'src'

$testCommand = $null
$testArguments = @()
$candidates = @(
    [pscustomobject]@{ Command = 'python'; Arguments = @() }
    [pscustomobject]@{ Command = 'python3'; Arguments = @() }
    [pscustomobject]@{ Command = 'py'; Arguments = @('-3') }
)
foreach ($candidate in $candidates) {
    if (-not (Get-Command $candidate.Command -ErrorAction SilentlyContinue)) {
        continue
    }
    $command = $candidate.Command
    $arguments = $candidate.Arguments
    $compatible = $false
    try {
        & $command @arguments -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 12) else 1)' 2>$null
        $compatible = $LASTEXITCODE -eq 0
    } catch {
        $compatible = $false
    }
    if ($compatible) {
        $testCommand = $command
        $testArguments = $arguments
        break
    }
}
if ($null -eq $testCommand) {
    throw 'Python 3.12 or newer runtime not found'
}

& $testCommand @testArguments -m unittest discover -s (Join-Path $projectRoot 'tests') -v
$testExitCode = $LASTEXITCODE
exit $testExitCode
