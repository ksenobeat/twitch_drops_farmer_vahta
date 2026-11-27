@echo off
rem ASCII only: cmd reads .bat byte by byte, non-ASCII inside breaks parsing.
rem All human-facing messages are printed by Python, which handles UTF-8 fine.
cd /d "%~dp0"
title Vahta

if not exist "env\Scripts\python.exe" (
    echo Python environment not found. Run once:
    echo.
    echo     py -3.12 -m venv env
    echo     env\Scripts\python.exe -m pip install -r requirements.txt
    echo.
    pause
    exit /b 1
)

env\Scripts\python.exe -m vahta.cli serve --open
pause
