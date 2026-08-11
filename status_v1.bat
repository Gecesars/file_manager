@echo off
setlocal
cd /d "%~dp0"
docker compose ps
echo.
powershell -NoProfile -Command "$port=((Get-Content .env | Where-Object { $_ -match '^OFC_PUBLIC_PORT=' }) -split '=')[1]; if (-not $port) { $port='5090' }; try { Invoke-RestMethod ('http://127.0.0.1:'+$port+'/api/services') | ConvertTo-Json -Depth 8 } catch { Write-Error $_ }"
endlocal
