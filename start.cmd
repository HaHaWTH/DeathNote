@echo off
setlocal
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
    echo Run install.cmd first.
    exit /b 1
)
".venv\Scripts\python.exe" -B -X utf8 -m deathnote run --console --host 0.0.0.0 --dns-upstream 1.1.1.1 %*
exit /b %errorlevel%
