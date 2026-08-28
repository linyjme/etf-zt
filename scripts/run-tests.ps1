$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
$env:PYTHONPATH = Join-Path $projectRoot 'src'
if (Get-Command python -ErrorAction SilentlyContinue) {
    & python -m unittest discover -s (Join-Path $projectRoot 'tests') -v
} elseif (Get-Command py -ErrorAction SilentlyContinue) {
    & py -3 -m unittest discover -s (Join-Path $projectRoot 'tests') -v
} else {
    throw 'Python 3 runtime not found'
}
exit $LASTEXITCODE
