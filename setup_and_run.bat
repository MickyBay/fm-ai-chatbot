@echo off
setlocal enabledelayedexpansion
title FileMaker AI Chatbot - Setup and Run

echo ============================================
echo   FileMaker AI Chatbot - Setup and Run
echo ============================================
echo.

REM Always work from the folder this .bat file is in, no matter where
REM it's double-clicked from.
cd /d "%~dp0"

REM ---------------------------------------------------------------
REM 1. Check Python is installed
REM ---------------------------------------------------------------
where python >nul 2>nul
if errorlevel 1 (
    echo [ERROR] Python was not found on this computer.
    echo.
    echo Please install Python 3.10 or newer from:
    echo   https://www.python.org/downloads/
    echo.
    echo IMPORTANT: during installation, tick the box that says
    echo "Add Python to PATH", then run this file again.
    echo.
    pause
    exit /b 1
)

REM ---------------------------------------------------------------
REM 2. Create a virtual environment the first time only
REM ---------------------------------------------------------------
if not exist "venv\" (
    echo Setting up the app for the first time, please wait...
    python -m venv venv
)

REM ---------------------------------------------------------------
REM 3. Activate the virtual environment
REM ---------------------------------------------------------------
call venv\Scripts\activate.bat
if errorlevel 1 (
    echo [ERROR] Could not activate the Python environment.
    pause
    exit /b 1
)

REM ---------------------------------------------------------------
REM 4. Install / update required packages
REM ---------------------------------------------------------------
echo Installing required packages, this may take a minute...
python -m pip install --quiet --upgrade pip
pip install --quiet -r requirements.txt
if errorlevel 1 (
    echo [ERROR] Could not install required packages.
    echo Please check your internet connection and try again.
    pause
    exit /b 1
)

REM ---------------------------------------------------------------
REM 5. Create a starter config.json the first time only
REM ---------------------------------------------------------------
if not exist "config.json" (
    if exist "config.example.json" (
        copy /y config.example.json config.json >nul
        echo Created a fresh config.json from the template.
    ) else (
        echo {"projects": {}} > config.json
        echo Created a blank config.json.
    )
)

echo.
echo ============================================
echo   Starting the FileMaker AI Chatbot...
echo   Your browser will open automatically at:
echo   http://127.0.0.1:8000
echo.
echo   Keep this black window open while you use
echo   the app. Closing it will stop the chatbot.
echo ============================================
echo.

REM ---------------------------------------------------------------
REM 6. Open the browser a few seconds after the server starts, then
REM    start the server itself (this line blocks until closed).
REM ---------------------------------------------------------------
start "" cmd /c "timeout /t 3 >nul && start http://127.0.0.1:8000"
uvicorn main:app --host 127.0.0.1 --port 8000

echo.
echo The chatbot has stopped.
pause