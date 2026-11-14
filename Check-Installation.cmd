@echo off
setlocal
cd /d "%~dp0"
set PYTHONUTF8=1
set PYTHONIOENCODING=utf-8
"%~dp0runtime\python\python.exe" -I -X utf8 -m app.selfcheck --full
pause
endlocal
