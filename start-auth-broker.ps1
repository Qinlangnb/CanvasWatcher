param([int]$Port = 8765, [string]$Backend = 'http://127.0.0.1:8000', [string]$Origins = 'http://localhost:8080,http://127.0.0.1:8080')
$ErrorActionPreference = 'Stop'
$env:AW_BROKER_PORT = "$Port"
$env:AW_BROKER_BACKEND = $Backend
$env:AW_BROKER_ORIGINS = $Origins
$brokerPython = Join-Path $PSScriptRoot 'auth-broker\.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $brokerPython)) {
    throw 'First run: py -3.12 -m venv auth-broker/.venv; auth-broker/.venv/Scripts/python -m pip install -e ./auth-broker; auth-broker/.venv/Scripts/python -m playwright install chromium'
}
& $brokerPython -m academic_watcher_auth_broker
exit $LASTEXITCODE
