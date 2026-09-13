@echo off
rem Copies everything the assistant has learned and configured to D:\adk-stock-chatbot-backup\<date>.
rem Run it by hand or schedule it (see README). None of these files are in GitHub, so this is the only copy.
cd /d "%~dp0"
set DEST=D:\adk-stock-chatbot-backup\%date:~-4%-%date:~-7,2%-%date:~-10,2%
mkdir "%DEST%" 2>nul
xcopy /E /I /Y /Q data "%DEST%\data" >nul
xcopy /E /I /Y /Q logs "%DEST%\logs" >nul
copy /Y knowledge.py "%DEST%\" >nul
copy /Y .env "%DEST%\" >nul
copy /Y HANDOFF.md "%DEST%\" >nul
echo Backed up to %DEST%