@echo off
REM Publish ΜΟΝΟ runtime στο P:\Dretaki (χωρίς src/sql/sln).
set ROOT=%~dp0
set DEST=P:\Dretaki\AutomationSchedulerFinal
set OUT=%DEST%\publish
set LOCALOUT=%ROOT%_publish_local

if not exist "P:\Dretaki" (
  echo Δεν βρέθηκε P:\Dretaki
  exit /b 1
)

echo Sync python -^> %DEST%\python ...
if not exist "%DEST%" mkdir "%DEST%"
if not exist "%DEST%\python" mkdir "%DEST%\python"
robocopy "%ROOT%python\katagrafi_cepal" "%DEST%\python\katagrafi_cepal" /E /XD chrome-profile chrome-profile-probe chrome-profile-probe2 chrome-profile-login-fresh __pycache__ .pytest_cache .venv /NFL /NDL /NJH /NJS /nc /ns /np
robocopy "%ROOT%python\agora_paravolon_dikastiria" "%DEST%\python\agora_paravolon_dikastiria" /E /XD chrome-profile __pycache__ .pytest_cache .venv /NFL /NDL /NJH /NJS /nc /ns /np
robocopy "%ROOT%python\agora_paravolon_klironomikes" "%DEST%\python\agora_paravolon_klironomikes" /E /XD chrome-profile __pycache__ .pytest_cache .venv /NFL /NDL /NJH /NJS /nc /ns /np

copy /Y "%ROOT%install-autostart.bat" "%DEST%\" >nul
copy /Y "%ROOT%install-autostart-logon.bat" "%DEST%\" >nul
copy /Y "%ROOT%uninstall-autostart.bat" "%DEST%\" >nul
copy /Y "%ROOT%publish.bat" "%DEST%\" >nul
copy /Y "%ROOT%setup-python.bat" "%DEST%\" >nul

REM Στο P:\ μένει μόνο runtime — όχι source tree
if exist "%DEST%\src" rmdir /s /q "%DEST%\src"
if exist "%DEST%\sql" rmdir /s /q "%DEST%\sql"
if exist "%DEST%\_publish_local" rmdir /s /q "%DEST%\_publish_local"
if exist "%DEST%\publish_new" rmdir /s /q "%DEST%\publish_new"
if exist "%DEST%\apply-new-publish.bat" del /f /q "%DEST%\apply-new-publish.bat"
if exist "%DEST%\AutomationSchedulerV3.sln" del /f /q "%DEST%\AutomationSchedulerV3.sln"
if exist "%DEST%\README.md" del /f /q "%DEST%\README.md"

echo Ensuring shared Python venv on P:\ ...
call "%ROOT%setup-python.bat"
if errorlevel 1 (
  echo setup-python failed
  exit /b 1
)

echo.
echo Publishing locally -^> %LOCALOUT% ...
if exist "%LOCALOUT%" rmdir /s /q "%LOCALOUT%"
mkdir "%LOCALOUT%"

dotnet publish "%ROOT%src\AutomationScheduler\AutomationScheduler.csproj" ^
  -c Release ^
  -r win-x64 ^
  --self-contained true ^
  -p:PublishSingleFile=false ^
  -o "%LOCALOUT%"

if errorlevel 1 (
  echo Publish failed.
  exit /b 1
)

powershell -NoProfile -Command ^
  "$p='%LOCALOUT%\appsettings.json'; $j=Get-Content $p -Raw -Encoding UTF8 | ConvertFrom-Json; $j.Scheduler.PythonExecutable='P:\Dretaki\AutomationSchedulerFinal\python\.venv\Scripts\python.exe'; foreach($x in $j.Scheduler.PythonJobs){ if($x.Action -like '*KATAGRAFI*'){ $x.WorkingDirectory='P:\Dretaki\AutomationSchedulerFinal\python\katagrafi_cepal' } elseif($x.Action -like '*DIKASTIRIA*AGORA*'){ $x.WorkingDirectory='P:\Dretaki\AutomationSchedulerFinal\python\agora_paravolon_dikastiria' } elseif($x.Action -like '*KLIRONOMIKES*AGORA*'){ $x.WorkingDirectory='P:\Dretaki\AutomationSchedulerFinal\python\agora_paravolon_klironomikes' } }; $j | ConvertTo-Json -Depth 20 | Set-Content $p -Encoding UTF8"

> "%LOCALOUT%\start-scheduler.bat" (
  echo @echo off
  echo cd /d "%%~dp0"
  echo start "" "%%~dp0AutomationScheduler.exe"
  echo exit /b 0
)

echo Copy to %OUT% ...
if not exist "%OUT%" mkdir "%OUT%"
robocopy "%LOCALOUT%" "%OUT%" /E /XD Logs /NFL /NDL /NJH /NJS /nc /ns /np
if exist "%OUT%\replace-exe.bat" del /f /q "%OUT%\replace-exe.bat"

> "%DEST%\README.txt" (
  echo Automation Scheduler ^(RDP^)
  echo.
  echo Τρέξιμο ^(αόρατο^): publish\start-scheduler.bat
  echo Logs: publish\Logs\
  echo Python setup: setup-python.bat
  echo Autostart login: install-autostart-logon.bat
)

echo.
echo OK: %OUT%\AutomationScheduler.exe
echo Start: %OUT%\start-scheduler.bat
exit /b 0
