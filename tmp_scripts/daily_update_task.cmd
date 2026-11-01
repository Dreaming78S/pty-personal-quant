@echo off
set PYTHONIOENCODING=utf-8
set UV=%USERPROFILE%\.local\bin\uv.exe
if not exist "%UV%" set UV=uv
cd /d "%~dp0.."
"%UV%" run python "%~dp0daily_update.py" >> logs\daily_update.log 2>&1
