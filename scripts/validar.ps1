$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath (Split-Path -Parent $PSScriptRoot)

$python = Join-Path (Get-Location) '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $python -PathType Leaf)) {
    py -3.12 -m venv .venv
    & $python -m pip install --upgrade pip
}
& $python -m pip install -e '.[dev]'
if ($LASTEXITCODE -ne 0) { throw 'Instalacao das dependencias falhou.' }
& $python -m pytest
if ($LASTEXITCODE -ne 0) { throw 'Testes Python falharam.' }
node --check src\ofc_media\static\app.js
if ($LASTEXITCODE -ne 0) { throw 'JavaScript invalido.' }
& (Join-Path $PSScriptRoot 'configurar.ps1')
docker compose config --quiet
if ($LASTEXITCODE -ne 0) { throw 'Compose invalido.' }
docker compose -f compose.yaml -f compose.gpu.yaml config --quiet
if ($LASTEXITCODE -ne 0) { throw 'Override opcional de GPU invalido.' }
Write-Host 'Validacao do File Manager concluida.' -ForegroundColor Green
