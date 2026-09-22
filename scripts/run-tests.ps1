$ErrorActionPreference = 'Stop'
$utf8NoBom = New-Object System.Text.UTF8Encoding $false
[Console]::OutputEncoding = $utf8NoBom
$OutputEncoding = $utf8NoBom

function Test-CompatiblePythonRuntime {
    param(
        [Parameter(Mandatory)]
        [string]$Command,
        [string[]]$Arguments = @(),
        [Parameter(Mandatory)]
        [string]$ProbeCode,
        [Parameter(Mandatory)]
        [string]$ExpectedOutput
    )

    $encodedProbe = [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($ProbeCode))
    $bootstrap = "import base64;exec(base64.b64decode('$encodedProbe'))"
    $startInfo = New-Object System.Diagnostics.ProcessStartInfo
    $startInfo.FileName = $Command
    $startInfo.Arguments = (@($Arguments) + @('-c', ('"' + $bootstrap + '"'))) -join ' '
    $startInfo.UseShellExecute = $false
    $startInfo.CreateNoWindow = $true
    $startInfo.RedirectStandardOutput = $true
    $startInfo.RedirectStandardError = $true
    $process = New-Object System.Diagnostics.Process
    $process.StartInfo = $startInfo
    try {
        if (-not $process.Start()) { return $false }
        $standardOutput = $process.StandardOutput.ReadToEnd()
        $process.StandardError.ReadToEnd() | Out-Null
        $process.WaitForExit()
        return $process.ExitCode -eq 0 -and $standardOutput.Trim() -ceq $ExpectedOutput
    } catch {
        return $false
    } finally {
        $process.Dispose()
    }
}

$projectRoot = Split-Path -Parent $PSScriptRoot
$env:PYTHONPATH = Join-Path $projectRoot 'src'

$testCommand = $null
$testArguments = @()
$probeMarker = "__ETF_ROTATION_PYTHON_312_$([guid]::NewGuid().ToString('N'))__"
$probeCode = "import sys; sys.exit(1) if sys.version_info < (3, 12) else print('$probeMarker')"
$candidates = @()
if ($env:VIRTUAL_ENV) {
    $candidates += [pscustomobject]@{
        Command = Join-Path $env:VIRTUAL_ENV 'Scripts\python.exe'
        Arguments = @()
    }
}
$candidates += [pscustomobject]@{
    Command = Join-Path $HOME '.workbuddy\binaries\python\envs\default\Scripts\python.exe'
    Arguments = @()
}
$candidates += @(
    [pscustomobject]@{ Command = 'python'; Arguments = @() }
    [pscustomobject]@{ Command = 'python3'; Arguments = @() }
    [pscustomobject]@{ Command = 'py'; Arguments = @('-3') }
)
foreach ($candidate in $candidates) {
    $applications = @()
    if ([IO.Path]::IsPathRooted($candidate.Command)) {
        if (Test-Path -LiteralPath $candidate.Command -PathType Leaf) {
            $applications = @([pscustomobject]@{ Source = $candidate.Command })
        }
    } else {
        $applications = @(Get-Command $candidate.Command -CommandType Application -All -ErrorAction SilentlyContinue)
    }
    foreach ($application in $applications) {
        $command = $application.Source
        $arguments = $candidate.Arguments
        if (Test-CompatiblePythonRuntime -Command $command -Arguments $arguments -ProbeCode $probeCode -ExpectedOutput $probeMarker) {
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
