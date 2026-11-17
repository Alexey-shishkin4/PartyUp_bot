param([switch]$Demo)
$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
$venvPython = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $venvPython)) {
    if (Get-Command uv -ErrorAction SilentlyContinue) {
        uv venv .venv --python 3.10
    } else {
        python -m venv .venv
    }
    if ($LASTEXITCODE -ne 0) { throw 'Не удалось создать Python-окружение' }
}
$requirementHash = (Get-FileHash -LiteralPath 'requirements.txt' -Algorithm SHA256).Hash
$stampPath = Join-Path $PSScriptRoot '.venv\requirements.sha256'
if (-not (Test-Path -LiteralPath $stampPath) -or (Get-Content -LiteralPath $stampPath -Raw).Trim() -ne $requirementHash) {
    if (Get-Command uv -ErrorAction SilentlyContinue) {
        uv pip install --python $venvPython -r requirements.txt
    } else {
        & $venvPython -m pip install -r requirements.txt
    }
    if ($LASTEXITCODE -ne 0) { throw 'Не удалось установить зависимости' }
    Set-Content -LiteralPath $stampPath -Value $requirementHash
}
if ($Demo) {
    $env:FORCE_DEMO = 'true'
    $env:PORT = '8765'
    $env:PUBLIC_URL = 'http://127.0.0.1:8765'
}
Write-Host 'PartyUp запускается. Демо: http://127.0.0.1:8765 · Остановка: Ctrl+C'
& $venvPython -m app.main
