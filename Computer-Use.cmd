@echo off
rem ---------------------------------------------------------------------------
rem  Launch OpenCode with the computer-use ability for THIS session only.
rem  Nothing is installed or enabled globally; everything lives in .runtime\.
rem
rem  Do not cd to this file's folder: the launcher uses the caller's current
rem  directory as the default project folder. Internal paths are absolute.
rem ---------------------------------------------------------------------------
setlocal

powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\Start-ComputerUse.ps1" %*
set "RC=%ERRORLEVEL%"

if not "%RC%"=="0" (
    echo.
    echo Launcher exited with code %RC%.
    rem Pause only in an interactive console; scripted runs must not hang.
    powershell -NoProfile -Command "if (-not [Console]::IsInputRedirected -and [Environment]::UserInteractive) { Write-Host 'Press Enter to close...'; [void][Console]::ReadLine() }"
)
exit /b %RC%
