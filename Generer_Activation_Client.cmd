@echo off
REM MySchoolGN - Prepare un fichier d'activation .cmd a usage unique pour un client.
REM Reserve au developpeur (machine autorisee par license_manager.py).
setlocal
cd /d "%~dp0"
title MySchoolGN - Fichier d'activation client

set "PY=python"
if exist "venv\Scripts\python.exe" set "PY=venv\Scripts\python.exe"

"%PY%" generer_activation_cmd.py %*
if not errorlevel 1 explorer "%~dp0activations_clients"
echo.
pause
