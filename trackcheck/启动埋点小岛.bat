@echo off
chcp 65001 >nul
cd /d "%~dp0"
set SPEC=samples\trackcheck.yaml
if not "%~1"=="" set SPEC=%~1
python trackcheck_web.py --spec "%SPEC%"
pause
