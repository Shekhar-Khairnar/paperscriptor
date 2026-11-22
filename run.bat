@echo off
rem One-click start: creates a virtual env on first run, installs deps, starts the app.
cd /d "%~dp0"
if not exist .venv (
  echo Creating virtual environment...
  python -m venv .venv || goto :error
)
call .venv\Scripts\activate.bat
python -m pip install -q -r requirements.txt || goto :error
python setup_latex.py
python app.py
goto :eof

:error
echo.
echo Setup failed. Make sure Python 3.10+ is installed and on PATH.
pause
