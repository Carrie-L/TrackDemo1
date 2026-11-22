@echo off
chcp 65001 >nul
cd /d "%~dp0"
python "%~dp0build_exe.py" %*
if errorlevel 1 goto fail
if "%~1"=="" pause
exit /b 0

:fail
echo.
echo 打包失败，请查看上方报错信息。
if "%~1"=="" pause
exit /b 1
