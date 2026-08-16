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
rem  Each candidate is tested in a subroutine rather than inside a parenthesised
rem  block. Inside a block, %errorlevel% is expanded when the whole block is
rem  parsed, not when the line runs, so it reports a stale value and the wrong
rem  branch is taken. A subroutine runs one statement at a time and sidesteps it.
set "PYEXE="
call :try_python "py -3"
if not defined PYEXE call :try_python "python"
if not defined PYEXE call :try_python "python3"
goto :python_found

:try_python
if defined PYEXE exit /b 0
%~1 -c "import sys; sys.exit(0 if sys.version_info[:2]>=(3,10) else 1)" >nul 2>&1
if errorlevel 1 exit /b 0
set "PYEXE=%~1"
exit /b 0

:python_found
if not defined PYEXE (
  echo   PROBLEM: no Python 3.10 or newer was found on this computer.
  echo.
  echo   Install it from  https://www.python.org/downloads/
  echo   During installation, tick "Add python.exe to PATH".
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
