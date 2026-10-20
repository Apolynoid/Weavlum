@echo off
setlocal
title Weavlum Cloudflare Tunnel (close this window to stop)
REM ============================================================
REM  Weavlum standalone public tunnel (Cloudflare Quick Tunnel)
REM  Usage:
REM    tunnel.cmd              start tunnel (default port 8000)
REM    tunnel.cmd 8010         start tunnel on custom port
REM    tunnel.cmd --status     show whether tunnel is running
REM    tunnel.cmd --stop       stop the tunnel process
REM  Start the Weavlum server first and keep it running.
REM  First run downloads cloudflared into data\bin (about 53 MB).
REM  The https://<random>.trycloudflare.com URL below is your public address.
REM  Closing this window (or Ctrl+C) stops the tunnel.
REM ============================================================
if /i "%~1"=="--stop" goto stop
if /i "%~1"=="--status" goto status

set "PORT=%~1"
if "%PORT%"=="" set "PORT=8000"
set "ROOT=%~dp0.."
set "DIR=%ROOT%\data\bin"
set "BIN=%DIR%\cloudflared.exe"

if not exist "%DIR%" mkdir "%DIR%"

call :is_running
if not errorlevel 1 goto already_running

where cloudflared >nul 2>&1
if not errorlevel 1 set "BIN=cloudflared"
if /i "%BIN%"=="cloudflared" goto run

echo Checking cloudflared ...
"%BIN%" --version >nul 2>&1
if not errorlevel 1 goto run

echo.
echo First run: downloading cloudflared (about 53 MB). Please wait for progress.
call :dl "https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-windows-amd64.exe" "[1/3] GitHub direct"
if not errorlevel 1 goto ready
call :dl "https://gh-proxy.com/https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-windows-amd64.exe" "[2/3] GitHub blocked, using mirror gh-proxy.com"
if not errorlevel 1 goto ready
call :dl "https://ghproxy.net/https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-windows-amd64.exe" "[3/3] using mirror ghproxy.net"
if not errorlevel 1 goto ready

echo.
echo [ERROR] cloudflared download failed (network blocked).
echo         Manual fix: download cloudflared-windows-amd64.exe and save it as
echo         %BIN%
pause
exit /b 1

:dl
echo %~2 (max 90s) ...
del /q "%BIN%" >nul 2>&1
curl.exe -fL -# --connect-timeout 10 --max-time 90 -o "%BIN%" "%~1"
if errorlevel 1 goto dl_fail
for %%A in ("%BIN%") do if %%~zA LSS 20000000 goto dl_fail
set "TRY=0"
:dl_verify
"%BIN%" --version >nul 2>&1
if not errorlevel 1 exit /b 0
set /a TRY+=1
if %TRY% GEQ 4 goto dl_fail
timeout /t 2 /nobreak >nul
goto dl_verify

:dl_fail
del /q "%BIN%" >nul 2>&1
exit /b 1

:ready
echo [ok] cloudflared ready.
:run
echo.
echo Tunnel: http://127.0.0.1:%PORT%  --^>  Cloudflare
echo Wait for the https://^<random^>.trycloudflare.com URL below, share it.
echo Stop later with: tunnel.cmd --stop  (or close this window)
echo.
"%BIN%" tunnel --url "http://127.0.0.1:%PORT%" --protocol http2 --no-autoupdate
echo.
echo Tunnel stopped.
pause >nul
exit /b 0

:already_running
echo [WARN] A Weavlum tunnel is already running.
echo        Stop it first with:  scripts\tunnel.cmd --stop
pause
exit /b 1

:stop
echo Stopping Weavlum tunnel ...
powershell -NoProfile -Command "$p=@(Get-CimInstance Win32_Process | Where-Object { $_.Name -eq 'cloudflared.exe' -and $_.CommandLine -like '*tunnel --url*' }); if ($p.Count -eq 0) { Write-Host 'No tunnel process found.' } else { foreach ($x in $p) { Write-Host ('Stopping PID ' + $x.ProcessId); Stop-Process -Id $x.ProcessId -Force -ErrorAction SilentlyContinue }; Write-Host 'Tunnel stopped.' }"
exit /b 0

:status
powershell -NoProfile -Command "$p=@(Get-CimInstance Win32_Process | Where-Object { $_.Name -eq 'cloudflared.exe' -and $_.CommandLine -like '*tunnel --url*' }); if ($p.Count -eq 0) { Write-Host 'Tunnel: stopped' } else { foreach ($x in $p) { Write-Host ('Tunnel: running (PID ' + $x.ProcessId + ')') } }"
exit /b 0

:is_running
powershell -NoProfile -Command "$p=@(Get-CimInstance Win32_Process | Where-Object { $_.Name -eq 'cloudflared.exe' -and $_.CommandLine -like '*tunnel --url*' }); if ($p.Count -eq 0) { exit 1 } else { exit 0 }"
exit /b %errorlevel%
