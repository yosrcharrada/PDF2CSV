@echo off
rem ===========================================================================
rem  PDF2CSV - start the web interface (development checkout)
rem
rem  Double-click this after setup.bat has been run once.
rem
rem  This is the development launcher. The one that ships to an analyst is
rem  packaging\bundle\Start PDF2CSV.bat, which uses a bundled Python instead of
rem  a virtual environment.
rem ===========================================================================

setlocal
title PDF to CSV
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
  echo.
  echo   This project has not been set up yet.
  echo.
  echo   Double-click  setup.bat  first. It takes a few minutes and only
  echo   needs doing once.
  echo.
  pause
  exit /b 1
)

rem Keep the interpreter isolated from any other Python on this machine, and
rem behave the same on every Windows locale.
set "PYTHONNOUSERSITE=1"
set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"

echo.
echo   Starting PDF to CSV...
echo   Your browser will open in a moment.
echo.
echo   Leave this window open while you work. Close it to stop.
echo.

".venv\Scripts\python.exe" -m pdf2csv ui

echo.
if errorlevel 1 (
  echo   ---------------------------------------------------------------
  echo    PDF to CSV stopped because of a problem.
  echo    The message above, and logs\pdf2csv.log, explain why.
  echo   ---------------------------------------------------------------
) else (
  echo   PDF to CSV has stopped. You can close this window.
)
echo.
pause
