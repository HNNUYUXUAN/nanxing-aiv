$ErrorActionPreference = 'Stop'
$projectPython = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $projectPython)) { $projectPython = 'python' }
& $projectPython (Join-Path $PSScriptRoot 'scripts\reproduce.py') @args
exit $LASTEXITCODE
