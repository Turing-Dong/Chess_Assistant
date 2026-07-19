@echo off
setlocal

cd /d "%~dp0"

if not exist ".env" (
    echo Missing .env. Creating it from .env.example...
    copy ".env.example" ".env" >nul
    echo.
    echo Please fill .env with your OSS bucket, device id list, and credentials.
    echo The file will open now. Save it, close Notepad, then run this bat again.
    notepad ".env"
    pause
    exit /b 1
)

if not exist ".venv\Scripts\python.exe" (
    echo Creating Python virtual environment...
    py -3 -m venv ".venv" 2>nul
    if errorlevel 1 (
        python -m venv ".venv"
    )
    if errorlevel 1 (
        echo Failed to create virtual environment. Install Python 3 and try again.
        pause
        exit /b 1
    )
)

call ".venv\Scripts\activate.bat"

echo Installing or updating Python dependencies...
python -m pip install -r requirements.txt
if errorlevel 1 (
    echo Failed to install dependencies.
    pause
    exit /b 1
)

echo Starting OSS AI worker...
python worker.py --env-file ".env" --debug-dir debug

echo.
echo Worker exited.
pause
