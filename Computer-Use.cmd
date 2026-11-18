@echo off
rem ---------------------------------------------------------------------------
rem  Launch OpenCode with the computer-use ability for THIS session only.
rem  Nothing is installed or enabled globally; everything lives in .runtime\.
rem ---------------------------------------------------------------------------
setlocal
cd /d "%~dp0"

powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\Start-ComputerUse.ps1" %*
set "RC=%ERRORLEVEL%"

if not "%RC%"=="0" (
    echo.
    echo Launcher exited with code %RC%.
    pause
)
exit /b %RC%
