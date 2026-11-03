@echo off
cd /d "%~dp0"
REM Relative paths — δουλεύει από mapped drive ή UNC.
set "ROOT=%~dp0.."
set "PY=%ROOT%\.venv\Scripts\python.exe"
set "PLAYWRIGHT_BROWSERS_PATH=%ROOT%\ms-playwright"
set "TCL_LIBRARY=%ROOT%\runtime\tcl\tcl8.6"
set "TK_LIBRARY=%ROOT%\runtime\tcl\tk8.6"

if not exist "%PY%" (
  echo Λείπει το venv:
  echo   %PY%
  echo Τρέξε: setup-python.bat από AutomationSchedulerFinal
  pause
  exit /b 1
)

"%PY%" -c "import sys; print(sys.version)" >nul 2>&1
if errorlevel 1 (
  echo Το Python του venv δεν ανοίγει.
  echo PY=%PY%
  "%PY%" -c "import sys; print(sys.version)"
  pause
  exit /b 1
)

"%PY%" main.py %*
set ERR=%ERRORLEVEL%
if not "%ERR%"=="0" (
  echo.
  echo Η καταχώρηση έκλεισε με σφάλμα ExitCode=%ERR%
  echo %* | findstr /I /C:"--scheduled" >nul
  if errorlevel 1 pause
)
exit /b %ERR%
