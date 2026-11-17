@echo off
rem FlashRel Rig launcher (ISE 16:540:585 Project 4, run 2).
rem Starts the rig in the background and opens the dashboard in the browser.
rem Running it again while the rig runs just opens the dashboard again.
setlocal EnableExtensions
cd /d "%~dp0"
title FlashRel Rig

set "RESUME="
set "NOADMIN="
set "ELEVATED="
for %%A in (%*) do (
  if /i "%%~A"=="--resume" set "RESUME=--resume"
  if /i "%%~A"=="--no-admin" set "NOADMIN=1"
  if /i "%%~A"=="--elevated" set "ELEVATED=1"
)

rem A stop request left over from an earlier session must not stop this start.
if exist "data\STOP" del /q "data\STOP" >nul 2>&1

rem ---- 1. administrator rights: formatting, USB device restart, PC settings ----
fltmc >nul 2>&1
if not errorlevel 1 goto :admin_ok
if defined NOADMIN goto :admin_ok
if defined ELEVATED goto :admin_refused
echo Asking Windows for administrator rights ...
set "FR_BAT=%~f0"
set "FR_ARGS=--elevated %RESUME%"
rem cmd /s /c ""<bat>" <args>" keeps a path with spaces, ( ) or & intact; [char]34 is a double quote.
powershell -NoProfile -ExecutionPolicy Bypass -Command "$q=[char]34; try { Start-Process -FilePath $env:ComSpec -ArgumentList ('/s /c ' + $q + $q + $env:FR_BAT + $q + ' ' + $env:FR_ARGS + $q) -Verb RunAs -ErrorAction Stop; exit 0 } catch { exit 1 }"
if not errorlevel 1 exit /b 0
:admin_refused
echo.
echo Administrator rights were refused. The rig starts WITHOUT them:
echo drives must then be formatted by hand and the recovery protocol cannot restart a drive.
echo.
:admin_ok

rem ---- 2. the Python runtime that matches this PC ----
set "RT=%~dp0runtime\amd64"
if /i "%PROCESSOR_ARCHITECTURE%"=="ARM64" if exist "%~dp0runtime\arm64\python.exe" set "RT=%~dp0runtime\arm64"
if not exist "%RT%\python.exe" (
  echo The Python runtime is missing: "%RT%". Copy the whole FlashRel-Rig folder again.
  pause
  exit /b 1
)

rem ---- 3. quick self-check, then start the rig in the background ----
"%RT%\python.exe" -m flashrel preflight
if errorlevel 1 (
  echo.
  echo The rig cannot start: see the message above.
  pause
  exit /b 1
)
if exist "%~dp0data\dashboard_url.txt" del /q "%~dp0data\dashboard_url.txt" >nul 2>&1
set "FLASHREL_LAUNCHER=%~f0"
start "" "%RT%\pythonw.exe" -m flashrel app %RESUME%

rem ---- 4. wait until the dashboard answers ----
for /l %%i in (1,1,40) do (
  if exist "%~dp0data\dashboard_url.txt" goto :started
  timeout /t 1 /nobreak >nul
)
echo.
echo The rig did not report back within 40 seconds. Last lines of data\controller.log:
powershell -NoProfile -Command "if (Test-Path 'data\controller.log') { Get-Content -Encoding UTF8 -Tail 25 'data\controller.log' }"
pause
exit /b 1

:started
set /p URL=<"%~dp0data\dashboard_url.txt"
echo.
echo   FlashRel Rig is running:  %URL%
echo   The dashboard opens in your browser. You can close this window;
echo   the test keeps running. Run this file again to reopen the dashboard.
echo.
timeout /t 10 >nul
exit /b 0
