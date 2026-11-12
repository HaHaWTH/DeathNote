@echo off
setlocal
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
    if "%~1"=="" (
        py -3.12 -m venv .venv
    ) else (
        "%~1" -m venv .venv
    )
    if errorlevel 1 exit /b 1
)
".venv\Scripts\python.exe" -m pip --disable-pip-version-check --no-cache-dir install -r requirements.txt
exit /b %errorlevel%
