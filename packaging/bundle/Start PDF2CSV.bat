@echo off
rem ===========================================================================
rem  PDF to CSV - start the application
rem
rem  This is the only file the analyst runs. It must work by double-clicking,
rem  on a standard user account, with no Python, no admin rights and no
rem  internet connection.
rem
rem  Every path is relative to this file's folder, so the whole thing can live
rem  on a desktop, a USB stick or a network share without being reconfigured.
rem ===========================================================================

title PDF to CSV
cd /d "%~dp0"

rem --- Keep everything inside this folder -----------------------------------
set "PDF2CSV_HOME=%~dp0"

rem --- Refuse to phone home --------------------------------------------------
rem  Belt and braces. Nothing in the shipped set should reach the network, but
rem  these are the three environment variables that stop the usual suspects
rem  from trying, and a blocked attempt on a client's machine is a security
rem  conversation nobody wants to have.
set "HF_HUB_OFFLINE=1"
set "TRANSFORMERS_OFFLINE=1"
set "NO_PROXY=*"

rem --- Isolate from any Python already on this machine ----------------------
rem  This is the one that bites hardest and is hardest to diagnose.
rem
rem  The bundled runtime has to enable `import site` or nothing imports at all,
rem  and that also switches on the per-user site-packages folder
rem  (%APPDATA%\Python\Python311\site-packages). If the analyst's machine has
rem  any Python 3.11 packages there - installed years ago, by someone else, for
rem  something else - they land on this application's import path and can
rem  shadow the versions that shipped in this folder.
rem
rem  The symptom is an import error or a wrong-version crash that reproduces on
rem  exactly one desktop and nowhere else.
set "PYTHONNOUSERSITE=1"
set "PYTHONPATH="
set "PYTHONHOME="

rem --- Behave predictably on any Windows locale ------------------------------
rem  Without PYTHONUTF8, a French or Arabic statement can fail to write its CSV
rem  on a machine whose codepage is not UTF-8.
set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"

rem  Do not litter the bundle with .pyc files, which also keeps it read-only
rem  friendly for network-share deployments.
set "PYTHONDONTWRITEBYTECODE=1"

rem --- Sanity check before we blame Python -----------------------------------
if not exist "%~dp0python\python.exe" (
  echo.
  echo   This folder looks incomplete - the "python" folder is missing.
  echo.
  echo   If you copied this from a zip file, make sure you extracted the
  echo   WHOLE folder rather than dragging files out of the zip viewer.
  echo.
  pause
  exit /b 1
)

echo.
echo   Starting PDF to CSV...
echo   Your browser will open in a moment.
echo.

"%~dp0python\python.exe" -m pdf2csv ui

rem --- Hold the window open --------------------------------------------------
rem  If this crashes on a client's desktop, this window holds the only
rem  evidence that will ever exist. Do not remove the pause.
echo.
if errorlevel 1 (
  echo   ---------------------------------------------------------------
  echo    PDF to CSV stopped because of a problem.
  echo.
  echo    Please send the message above, and the file
  echo      %~dp0logs\pdf2csv.log
  echo    to whoever set this up for you.
  echo   ---------------------------------------------------------------
) else (
  echo   PDF to CSV has stopped. You can close this window.
)
echo.
pause
