@echo off
REM Εναλλακτικό: ξεκινά ΜΕΤΑ το login (καλύτερο για Chrome/CEPAL).
REM Για 24/7 χωρίς Sign out: βάλε Windows auto-logon + αυτό το task.
set ROOT=%~dp0
set EXE=P:\Dretaki\AutomationSchedulerFinal\publish\AutomationScheduler.exe
if not exist "%EXE%" set EXE=%ROOT%publish\AutomationScheduler.exe
if not exist "%EXE%" set EXE=%ROOT%publish\AutomationScheduler\AutomationScheduler.exe
set TASK=KlironomikesAutomationScheduler

if not exist "%EXE%" (
  echo Δεν βρέθηκε %EXE%
  echo Τρέξε πρώτα publish.bat
  exit /b 1
)

schtasks /Delete /F /TN "%TASK%" >nul 2>&1
schtasks /Create /F /TN "%TASK%" /SC ONLOGON /DELAY 0000:30 /RL LIMITED /TR "\"%EXE%\""
if errorlevel 1 (
  echo Αποτυχία δημιουργίας scheduled task.
  exit /b 1
)

echo OK: ξεκινά 30'' μετά το login.
schtasks /Run /TN "%TASK%"
exit /b 0
