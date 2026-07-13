@echo off
setlocal
title Building FileMaker AI Chatbot .exe

echo ============================================
echo   Building a standalone .exe
echo   (run this once, on YOUR computer only -
echo    the end user never needs this file)
echo ============================================
echo.

cd /d "%~dp0"

REM Reuse the same virtual environment setup_and_run.bat already created.
REM If it doesn't exist yet, run setup_and_run.bat once first so all the
REM app's own packages (fastapi, uvicorn, httpx, pydantic) are installed.
if not exist "venv\" (
    echo [ERROR] No "venv" folder found yet.
    echo Please run setup_and_run.bat once first, then run this file again.
    pause
    exit /b 1
)

call venv\Scripts\activate.bat

echo Installing PyInstaller...
pip install --quiet pyinstaller

echo.
echo Building the .exe - this can take a few minutes...
pyinstaller --noconfirm --onefile --name "FileMaker-AI-Chatbot" ^
  --add-data "static;static" ^
  --add-data "config.example.json;." ^
  --collect-all uvicorn ^
  --collect-all fastapi ^
  --collect-all httpx ^
  main.py

if errorlevel 1 (
    echo.
    echo [ERROR] Build failed - scroll up to see what PyInstaller reported.
    pause
    exit /b 1
)

echo.
echo ============================================
echo   Done!
echo   Your standalone app is at:
echo   dist\FileMaker-AI-Chatbot.exe
echo.
echo   Send JUST that one .exe file to the end
echo   user - they double-click it and a browser
echo   window opens automatically. Nothing else
echo   to install.
echo ============================================
echo.
pause