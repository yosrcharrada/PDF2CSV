@echo off
rem ===========================================================================
rem  PDF2CSV - one-click development setup for Windows
rem
rem  Double-click this file. It creates a virtual environment in this folder,
rem  installs everything, and checks the result. Safe to run more than once.
rem
rem  It exists because the install is four commands and two of them are easy to
rem  get subtly wrong: installing into the wrong Python, and installing the OCR
rem  add-on in one step instead of two (which silently replaces the headless
rem  OpenCV with the GUI build).
rem ===========================================================================

setlocal
title PDF2CSV setup
cd /d "%~dp0"

echo.
echo   PDF2CSV - setting up
echo   ====================
echo.

rem --- 1. Find a usable Python ----------------------------------------------
rem  Needs 3.10, 3.11 or 3.12. Not 3.13+: several of the compiled dependencies
rem  have not been verified there, so pyproject.toml caps it and pip would
rem  otherwise fail later with "requires a different Python", which does not
rem  tell the user what to do about it.
rem
rem  The version-specific py-launcher aliases are tried first, so a machine with
rem  both 3.13 and 3.12 installed picks 3.12 automatically instead of failing.
rem
rem  Each candidate is tested in a subroutine rather than inside a parenthesised
rem  block. Inside a block, %errorlevel% is expanded when the whole block is
rem  parsed, not when the line runs, so it reports a stale value and the wrong
rem  branch is taken. A subroutine runs one statement at a time and sidesteps it.
set "PYEXE="
call :try_python "py -3.12"
call :try_python "py -3.11"
call :try_python "py -3.10"
call :try_python "python"
call :try_python "python3"
call :try_python "py -3"
goto :python_found

:try_python
if defined PYEXE exit /b 0
%~1 -c "import sys; sys.exit(0 if (3,10)<=sys.version_info[:2]<(3,13) else 1)" >nul 2>&1
if errorlevel 1 exit /b 0
set "PYEXE=%~1"
exit /b 0

:python_found
if not defined PYEXE (
  echo   PROBLEM: no suitable Python was found.
  echo.
  echo   This project needs Python 3.10, 3.11 or 3.12.
  echo   Python 3.13 and newer are NOT supported yet, and that is the usual
  echo   reason for seeing this message - the version on python.org's front
  echo   page is newer than what this project has been tested against.
  echo.
  echo   What is on this computer right now:
  python --version 2>nul
  py -0p 2>nul
  echo.
  echo   Fix: install Python 3.12 from
  echo     https://www.python.org/downloads/
  echo   Scroll to "Looking for a specific release" and choose the newest
  echo   3.12.x. Tick "Add python.exe to PATH" during installation.
  echo   Installing 3.12 alongside a newer Python is fine; this script will
  echo   find and use it automatically.
  echo.
  echo   Then run this file again.
  echo.
  pause
  exit /b 1
)

for /f "delims=" %%v in ('%PYEXE% -c "import sys;print(sys.version.split()[0])"') do set "PYVER=%%v"
echo   [1/5] Using Python %PYVER%

rem --- 2. Create the virtual environment ------------------------------------
if exist ".venv\Scripts\python.exe" (
  echo   [2/5] Virtual environment already exists - reusing it
) else (
  echo   [2/5] Creating virtual environment in .venv ...
  %PYEXE% -m venv .venv
  if errorlevel 1 (
    echo.
    echo   PROBLEM: could not create the virtual environment.
    pause
    exit /b 1
  )
)

set "VPY=%~dp0.venv\Scripts\python.exe"

rem --- 3. Core install -------------------------------------------------------
echo   [3/5] Installing the application and its dependencies ...
"%VPY%" -m pip install --upgrade pip setuptools wheel --quiet
"%VPY%" -m pip install -e ".[dev]" --quiet
if errorlevel 1 (
  echo.
  echo   PROBLEM: the main install failed. The most common causes are:
  echo     - no internet connection
  echo     - Windows long-path limit; try moving this folder somewhere shorter
  echo.
  pause
  exit /b 1
)

rem --- 4. OCR add-on, in two steps ------------------------------------------
rem  This MUST be two commands. rapidocr-onnxruntime declares a hard dependency
rem  on opencv-python, the GUI build, which owns the same cv2 module as
rem  opencv-python-headless and overwrites it. Installing it with --no-deps and
rem  declaring its real dependencies ourselves keeps the headless build.
echo   [4/5] Installing scanned-document support ...
"%VPY%" -m pip install -r requirements-ocr.txt --quiet
"%VPY%" -m pip install --no-deps -r requirements-ocr-nodeps.txt --quiet
if errorlevel 1 (
  echo.
  echo   Scanned-document support could not be installed.
  echo   Everything else still works; PDFs containing real text will convert
  echo   normally, and scanned ones will report a clear message.
  echo.
)

rem --- 5. Verify -------------------------------------------------------------
echo   [5/5] Checking the installation ...
echo.
"%VPY%" -m pdf2csv check
if errorlevel 1 (
  echo.
  echo   The check above reported a problem. Send it to whoever supports this.
  pause
  exit /b 1
)

echo.
echo   ===============================================================
echo    Setup finished.
echo.
echo    To start the app, double-click:   run.bat
echo.
echo    Or from a command prompt in this folder:
echo      .venv\Scripts\activate.bat
echo      python -m pdf2csv ui
echo   ===============================================================
echo.
pause
