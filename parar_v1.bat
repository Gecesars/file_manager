@echo off
setlocal
cd /d "%~dp0"
echo Esta acao para somente os containers do projeto file-manager.
docker compose stop
echo Volumes, PostgreSQL, downloads e cache foram preservados.
endlocal
