@echo off
REM Shared venv — ίδιο Python για scheduler / RDP / start.bat (edretaki + developer10).
REM Δουλεύει από mapped drive ή UNC χωρίς hardcode host.
set ROOT=%~dp0

REM Προτίμηση: φάκελος δίπλα στο setup.
set DEST=%ROOT%python
if not exist "%DEST%\runtime\python.exe" (
  echo Λείπει runtime δίπλα στο setup. Βάλε Python 3.12.7 στο python\runtime\
  pause
  exit /b 1
)
set RUNTIME=%DEST%\runtime\python.exe
set VENV=%DEST%\.venv
set PY=%VENV%\Scripts\python.exe

if not exist "%RUNTIME%" (
  echo Λείπει το shared runtime: %DEST%\runtime
  echo Αντιγράψτε Python 3.12.7 εκεί.
  pause
  exit /b 1
)

if not exist "%DEST%" mkdir "%DEST%"

echo Shared runtime:
"%RUNTIME%" --version
echo Creating/updating venv: %VENV%
"%RUNTIME%" -m venv "%VENV%"
if errorlevel 1 (
  echo venv failed
  pause
  exit /b 1
)

echo Installing requirements...
"%PY%" -m pip install --upgrade pip
"%PY%" -m pip install -r "%ROOT%python\katagrafi_cepal\requirements.txt"
"%PY%" -m pip install -r "%ROOT%python\agora_paravolon_dikastiria\requirements.txt"
"%PY%" -m pip install -r "%ROOT%python\agora_paravolon_klironomikes\requirements.txt"

set PLAYWRIGHT_BROWSERS_PATH=%DEST%\ms-playwright
echo Installing Chromium -^> %PLAYWRIGHT_BROWSERS_PATH%
"%PY%" -m playwright install chromium

echo.
echo OK: %PY%
"%PY%" -c "import sys, pyodbc, playwright; print(sys.version); print('imports ok')"
exit /b 0
