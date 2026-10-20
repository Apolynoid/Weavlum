@echo off
setlocal
title Weavlum Server Stop
chcp 65001 >nul
REM ============================================================
REM  Weavlum server stop (Windows, server runs inside WSL)
REM  Usage: double-click to stop, or:  stop.cmd --status
REM  Only stops this project's run.py (checked by cwd + cmdline).
REM ============================================================
set "WINPATH=%~dp0"
set "WINPATH=%WINPATH:~0,-1%"
for /f "usebackq delims=" %%p in (`wsl.exe wslpath -a "%WINPATH%"`) do set "WSLPATH=%%p"
if "%WSLPATH%"=="" (
  echo [ERROR] WSL not available.
  pause
  exit /b 1
)
set "ARG=%~1"
wsl.exe bash -lc "cd '%WSLPATH%' && ./stop.sh %ARG%"
echo.
pause >nul
