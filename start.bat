@echo off
REM Starts the Local TTS server and opens it in your browser.
cd /d "%~dp0"
if not exist .venv\Scripts\activate.bat (
  echo Run setup.bat first.
  pause
  exit /b 1
)
call .venv\Scripts\activate.bat
start "" http://localhost:48765
cd server
python -m uvicorn app:app --host 127.0.0.1 --port 48765
pause
