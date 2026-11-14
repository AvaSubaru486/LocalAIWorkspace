@echo off
setlocal
cd /d "%~dp0"
if not exist "%~dp0runtime\python\python.exe" (
  echo Bundled Python is missing. Extract the entire portable package.
  pause
  exit /b 1
)
set PYTHONUTF8=1
set PYTHONIOENCODING=utf-8
"%~dp0runtime\python\python.exe" -I -X utf8 -u -m app.launcher
if errorlevel 1 pause
endlocal
