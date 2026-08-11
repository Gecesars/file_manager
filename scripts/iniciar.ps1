$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath (Split-Path -Parent $PSScriptRoot)

& (Join-Path $PSScriptRoot 'configurar.ps1')

$configuration = Get-Content -LiteralPath '.env'
$driveRoot = ($configuration | Where-Object { $_ -match '^OFC_GDRIVE_ROOT_ID=' } | Select-Object -First 1) -replace '^OFC_GDRIVE_ROOT_ID=', ''
if (-not $driveRoot -or $driveRoot -eq 'CONFIGURE_O_ID_DA_PASTA' -or $driveRoot -eq 'YOUR_GOOGLE_DRIVE_FOLDER_ID') {
    throw 'Configure OFC_GDRIVE_ROOT_ID em .env antes de iniciar.'
}
if (-not (Test-Path -LiteralPath 'token.json' -PathType Leaf)) {
    throw 'token.json do Google Drive nao foi encontrado. Consulte o README.'
}

function Test-DockerReady {
    $oldPreference = $ErrorActionPreference
    $ErrorActionPreference = 'SilentlyContinue'
    try {
        docker info *> $null
        return $LASTEXITCODE -eq 0
    } finally {
        $ErrorActionPreference = $oldPreference
    }
}

if (-not (Test-DockerReady)) {
    $candidates = @(
        "$env:ProgramFiles\Docker\Docker\Docker Desktop.exe"
        "$env:LOCALAPPDATA\Docker\Docker Desktop.exe"
    )
    $desktop = $candidates | Where-Object { Test-Path -LiteralPath $_ -PathType Leaf } | Select-Object -First 1
    if (-not $desktop) { throw 'Docker Desktop nao foi localizado.' }
    Start-Process -FilePath $desktop -WindowStyle Hidden
    $deadline = [DateTime]::UtcNow.AddMinutes(3)
    do {
        Start-Sleep -Seconds 3
        $ready = Test-DockerReady
    } while (-not $ready -and [DateTime]::UtcNow -lt $deadline)
    if (-not $ready) { throw 'Docker Desktop nao ficou pronto em tres minutos.' }
}

docker compose config --quiet
if ($LASTEXITCODE -ne 0) { throw 'compose.yaml invalido.' }
docker compose up -d --build
if ($LASTEXITCODE -ne 0) { throw 'A stack nao iniciou.' }

$deadline = [DateTime]::UtcNow.AddMinutes(8)
do {
    Start-Sleep -Seconds 5
    $status = docker compose ps --format json | ConvertFrom-Json
    $gateway = @($status | Where-Object { $_.Service -eq 'gateway' -and $_.Health -eq 'healthy' })
} while (-not $gateway -and [DateTime]::UtcNow -lt $deadline)

docker compose ps
if (-not $gateway) { throw 'Gateway nao ficou saudavel; consulte docker compose logs.' }
$portLine = Get-Content -LiteralPath '.env' | Where-Object { $_ -match '^OFC_PUBLIC_PORT=' } | Select-Object -First 1
$port = if ($portLine) { $portLine.Split('=', 2)[1] } else { '5090' }
Write-Host "File Manager: http://127.0.0.1:$port" -ForegroundColor Green
