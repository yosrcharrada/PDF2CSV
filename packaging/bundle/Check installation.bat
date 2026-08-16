@echo off
rem ===========================================================================
rem  Diagnostics. Run this when PDF to CSV will not start, and send the output
rem  to whoever supports the tool.
rem
rem  Exists because the alternative is asking a non-technical user to open a
rem  terminal and describe what they see, which does not work.
rem ===========================================================================

title PDF to CSV - installation check
cd /d "%~dp0"

set "PDF2CSV_HOME=%~dp0"
set "HF_HUB_OFFLINE=1"
set "TRANSFORMERS_OFFLINE=1"

rem  Must match Start PDF2CSV.bat exactly, or this reports on a different
rem  environment than the one that is actually failing.
set "PYTHONNOUSERSITE=1"
set "PYTHONPATH="
set "PYTHONHOME="
set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"
set "PYTHONDONTWRITEBYTECODE=1"

echo.
echo   Checking this installation. This takes a few seconds.
echo.

if not exist "%~dp0python\python.exe" (
  echo   PROBLEM: the "python" folder is missing from
  echo            %~dp0
  echo.
  echo   The folder was probably copied incompletely. Extract the whole zip
  echo   again rather than dragging files out of the zip viewer.
  echo.
  pause
  exit /b 1
)

"%~dp0python\python.exe" -m pdf2csv check

echo.
echo   ---------------------------------------------------------------
echo    Copy everything above this line and send it to support.
echo   ---------------------------------------------------------------
echo.
pause
