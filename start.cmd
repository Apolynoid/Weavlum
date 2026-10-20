@echo off
setlocal
title Weavlum Server (use stop.cmd to stop)
chcp 65001 >nul
REM ============================================================
REM  Weavlum one-click start (double-click this file on Windows;
REM  the server runs inside WSL). Works from any drive letter --
REM  the path is converted by wslpath automatically.
REM  Closing this window may leave the server running in WSL;
REM  run stop.cmd (or: start.cmd --stop) to stop it.
REM  Also supports: start.cmd --status
REM ============================================================
if /i "%~1"=="--stop" goto stop_server
if /i "%~1"=="--status" goto status_server
set "WINPATH=%~dp0"
set "WINPATH=%WINPATH:~0,-1%"
for /f "usebackq delims=" %%p in (`wsl.exe wslpath -a "%WINPATH%"`) do set "WSLPATH=%%p"
if "%WSLPATH%"=="" (
  echo [ERROR] WSL not available. Install it first: wsl --install
  pause
  exit /b 1
)
echo Starting Weavlum (WSL path: %WSLPATH%) ...
echo.
wsl.exe bash -lc "cd '%WSLPATH%' && ./start.sh --open %*"
echo.
echo Server stopped. Press any key to close this window.
pause >nul
exit /b 0

:stop_server
call "%~dp0stop.cmd"
exit /b 0

:status_server
call "%~dp0stop.cmd" --status
exit /b 0
