@echo off
rem Double-click this file to open the screener dashboard (with its Run scan now button).
cd /d "%~dp0"
where py >nul 2>nul && (py screener\local.py serve) || (python screener\local.py serve)
pause
