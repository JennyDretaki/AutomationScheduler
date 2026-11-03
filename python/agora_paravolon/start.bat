@echo off
REM Παλιός φάκελος — η αγορά χωρίστηκε σε δύο.
REM Προεπιλογή: δικαστήριο (όπως ήταν το τελευταίο περιεχόμενο εδώ).
echo Σημείωση: χρησιμοποίησε agora_paravolon_dikastiria ή agora_paravolon_klironomikes.
cd /d "%~dp0..\agora_paravolon_dikastiria"
if errorlevel 1 (
  echo Δεν βρέθηκε agora_paravolon_dikastiria
  pause
  exit /b 1
)
call start.bat %*
