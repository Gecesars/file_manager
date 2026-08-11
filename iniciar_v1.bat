@echo off
setlocal
cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\iniciar.ps1"
if errorlevel 1 (
  echo ERRO: o File Manager nao iniciou. Os servicos existentes nao foram alterados.
  pause
  exit /b 1
)
set "FILE_MANAGER_PORT=5090"
for /f "tokens=2 delims==" %%P in ('findstr /b "OFC_PUBLIC_PORT=" ".env" 2^>nul') do set "FILE_MANAGER_PORT=%%P"
start "" "http://127.0.0.1:%FILE_MANAGER_PORT%"
endlocal
