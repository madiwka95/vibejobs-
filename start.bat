@echo off
rem VibeJobs — запуск в один клик: сервер + открыть браузер
cd /d "%~dp0"
where py >nul 2>nul && (set PY=py) || (set PY=python)
echo [*] VibeJobs: http://localhost:8000
start "" http://localhost:8000
%PY% app.py --port 8000
pause
