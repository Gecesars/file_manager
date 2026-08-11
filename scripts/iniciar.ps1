param(
    [switch]$Build,
    [switch]$Full
)

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

$imageReady = $false
$oldPreference = $ErrorActionPreference
$ErrorActionPreference = 'SilentlyContinue'
try {
    docker image inspect file-manager:2.0.0 *> $null
    $imageReady = $LASTEXITCODE -eq 0
} finally {
    $ErrorActionPreference = $oldPreference
}
if ($Build -or -not $imageReady) {
    Write-Host 'Construindo a imagem uma unica vez. Os proximos inicios reutilizarao esta imagem.' -ForegroundColor Yellow
    docker compose build control
    if ($LASTEXITCODE -ne 0) { throw 'A imagem da aplicacao nao foi construida.' }
}

function Start-ServicePhase([string[]]$Services) {
    docker compose up -d --no-build --no-deps @Services
    if ($LASTEXITCODE -ne 0) {
        throw "Falha ao iniciar: $($Services -join ', ')."
    }
}

function Wait-ServiceHealth([string]$Service, [int]$Seconds = 90) {
    $deadline = [DateTime]::UtcNow.AddSeconds($Seconds)
    do {
        $row = @(docker compose ps --format json $Service | ConvertFrom-Json)
        $healthy = @($row | Where-Object {
            $_.Service -eq $Service -and ($_.Health -eq 'healthy' -or (-not $_.Health -and $_.State -eq 'running'))
        })
        if ($healthy) { return }
        Start-Sleep -Seconds 3
    } while ([DateTime]::UtcNow -lt $deadline)
    throw "$Service nao ficou saudavel dentro do limite."
}

# Inicio em fases: evita o pico simultaneo de PostgreSQL, catalogo, FFmpeg e
# libtorrent. O modo padrao serve curadoria/Drive com menos de 1 GiB observado.
Start-ServicePhase @('postgres')
Wait-ServiceHealth 'postgres' 60
Start-ServicePhase @('redis')
Wait-ServiceHealth 'redis' 60

docker compose run --rm migrate
if ($LASTEXITCODE -ne 0) { throw 'A migracao do banco falhou.' }

Start-ServicePhase @('torrent-engine', 'gdrive-source')
Wait-ServiceHealth 'torrent-engine' 90
Wait-ServiceHealth 'gdrive-source' 90
Start-ServicePhase @('control')
Wait-ServiceHealth 'control' 90
Start-ServicePhase @('gateway')
Wait-ServiceHealth 'gateway' 60

if ($Full) {
    Write-Host 'Modo completo solicitado: iniciando transcodificacao e sincronizacao do catalogo.' -ForegroundColor Yellow
    Start-ServicePhase @('transcoder')
    Wait-ServiceHealth 'transcoder' 90
    Start-ServicePhase @('catalog-sync')
} else {
    Write-Host 'Modo leve: catalog-sync e transcoder permanecem desligados.' -ForegroundColor Cyan
}

docker compose ps
$portLine = Get-Content -LiteralPath '.env' | Where-Object { $_ -match '^OFC_PUBLIC_PORT=' } | Select-Object -First 1
$port = if ($portLine) { $portLine.Split('=', 2)[1] } else { '5090' }
Write-Host "File Manager: http://127.0.0.1:$port" -ForegroundColor Green
