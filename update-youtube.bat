@echo off
REM Updates the YouTube downloader (YouTube changes often).
cd /d "%~dp0"
call .venv\Scripts\activate.bat
pip install -U "yt-dlp[default]"
pause
