$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
$env:PYTHONPATH = Join-Path $projectRoot 'src'

$testCommand = $null
$testArguments = @()
$probeMarker = "__ETF_ROTATION_PYTHON_312_$([guid]::NewGuid().ToString('N'))__"
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
        $arguments = $candidate.Arguments
        $probeOutput = @()
        $probeExitCode = -1
        try {
            $global:LASTEXITCODE = -1
            $probeOutput = @(& $command @arguments -c $probeCode 2>$null)
            $probeExitCode = $LASTEXITCODE
        } catch {
            $probeOutput = @()
            $probeExitCode = -1
        }
        if ($probeExitCode -eq 0 -and $probeOutput.Count -eq 1 -and [string]$probeOutput[0] -ceq $probeMarker) {
            $testCommand = $command
            $testArguments = $arguments
            break
        }
    }
    if ($null -ne $testCommand) {
        break
    }
}
if ($null -eq $testCommand) {
    throw 'Python 3.12 or newer runtime not found'
}

$global:LASTEXITCODE = -1
& $testCommand @testArguments -m unittest discover -s (Join-Path $projectRoot 'tests') -v
$testExitCode = $LASTEXITCODE
exit $testExitCode
