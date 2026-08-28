$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$env:PYTHONPATH = Join-Path $root 'src'
if (Get-Command py -ErrorAction SilentlyContinue) {
    & py -3.14 -m unittest discover -s (Join-Path $root 'tests') -v
} elseif (Get-Command python -ErrorAction SilentlyContinue) {
    & python -m unittest discover -s (Join-Path $root 'tests') -v
} else {
    throw 'Python runtime not found'
}
exit $LASTEXITCODE
