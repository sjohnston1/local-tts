@echo off
REM One-time setup for Local TTS. Needs Python 3.12 (Kokoro doesn't support 3.13 yet).
cd /d "%~dp0"

py -3.12 --version >nul 2>&1
if errorlevel 1 (
  echo Python 3.12 was not found. Installing it with winget...
  winget install -e --id Python.Python.3.12 --scope user --accept-package-agreements --accept-source-agreements
  py -3.12 --version >nul 2>&1
  if errorlevel 1 (
    echo Automatic install failed. Install Python 3.12 from https://www.python.org/downloads/release/python-3128/
    echo and tick "Add python.exe to PATH" during install, then run this again.
    pause
    exit /b 1
  )
)

echo Creating virtual environment...
py -3.12 -m venv .venv
call .venv\Scripts\activate.bat
python -m pip install --upgrade pip

echo Installing PyTorch with CUDA 12.8 (needed for RTX 50-series cards)...
pip install torch --index-url https://download.pytorch.org/whl/cu128
if errorlevel 1 goto fail

echo Installing Kokoro and server packages...
pip install -r server\requirements.txt
if errorlevel 1 goto fail

python -c "import torch; print('CUDA available:', torch.cuda.is_available(), '-', torch.cuda.get_device_name(0) if torch.cuda.is_available() else 'CPU only')"
echo.
echo Setup done. Double-click start.bat to run Local TTS.
pause
exit /b 0

:fail
echo Setup failed - see the error above.
pause
exit /b 1
