$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath (Split-Path -Parent $PSScriptRoot)

$target = Join-Path (Get-Location) '.env'
if (Test-Path -LiteralPath $target -PathType Leaf) {
    Write-Host '.env ja configurado.'
    exit 0
}

function New-HexSecret([int]$Bytes = 32) {
    $buffer = New-Object byte[] $Bytes
    $generator = [System.Security.Cryptography.RandomNumberGenerator]::Create()
    try { $generator.GetBytes($buffer) } finally { $generator.Dispose() }
    return -join ($buffer | ForEach-Object { $_.ToString('x2') })
}

$lines = @(
    'POSTGRES_DB=ofc_media'
    'POSTGRES_USER=ofc'
    "POSTGRES_PASSWORD=$(New-HexSecret 24)"
    "OFC_INTERNAL_TOKEN=$(New-HexSecret 32)"
    "OFC_SESSION_PEPPER=$(New-HexSecret 32)"
    'OFC_PLAYBACK_TTL_SECONDS=43200'
    'OFC_PUBLIC_PORT=5090'
    'OFC_SYNC_INTERVAL=120'
    'OFC_TRANSCODE_ENCODER=auto'
    'OFC_MAX_TRANSCODES=1'
    'OFC_HLS_CACHE_GIB=250'
    'OFC_GDRIVE_ROOT_ID=CONFIGURE_O_ID_DA_PASTA'
    'OFC_GDRIVE_SYNC_INTERVAL=300'
)
[System.IO.File]::WriteAllLines($target, $lines, [System.Text.UTF8Encoding]::new($false))
Write-Host 'Segredos locais criados em .env.' -ForegroundColor Green
