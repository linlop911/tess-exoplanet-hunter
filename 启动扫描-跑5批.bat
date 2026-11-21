@echo off
chcp 65001 >nul
powershell -ExecutionPolicy Bypass -File "%~dp0start-scan.ps1" -Batches 5 -Pool hybrid
pause
