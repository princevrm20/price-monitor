@echo off
cd /d "%~dp0"
python -m price_monitor
if errorlevel 1 pause
