@echo off
rem Emergency stop of the rig software when the dashboard cannot be reached.
rem Every drive stops after the file it is writing; the cycles in progress are not counted.
rem Start FlashRel.bat continues the test afterwards (the 120-hour clock keeps running).
cd /d "%~dp0"
echo This stops the FlashRel software (the test can be continued with Start FlashRel.bat).
choice /m "Stop it now"
if errorlevel 2 exit /b 0
if not exist "data" mkdir "data"
echo stop> "data\STOP"
echo Stop requested. The workers finish their current file; this can take a few minutes.
timeout /t 10 >nul
