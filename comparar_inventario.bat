@echo off
setlocal
cd /d "%~dp0"
docker compose exec -T catalog-sync python3 -m ofc_media.compare_inventory
endlocal
