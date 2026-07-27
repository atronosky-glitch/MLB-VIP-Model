@echo off
setlocal

REM ── MLB VIP Model Launcher ──────────────────────────────────────
REM Double-click this file to start the control panel.

title MLB VIP Model

REM Determine repository directory
set "REPO_DIR=%~dp0"
cd /d "%REPO_DIR%"

REM Write launcher errors to log
set "LOG_DIR=%REPO_DIR%output"
if not exist "%LOG_DIR%" mkdir "%LOG_DIR%"
set "LOG_FILE=%LOG_DIR%\launcher.log"

REM Check Python
python --version >nul 2>&1
if %errorlevel% neq 0 (
    echo [ERROR] Python is not installed or not in PATH.
    echo [ERROR] Install Python 3.10+ from https://python.org
    echo [ERROR] See %LOG_FILE% for details.
    echo [%date% %time%] Python not found > "%LOG_FILE%"
    pause
    exit /b 1
)

REM Activate virtual environment if present
if exist "%REPO_DIR%venv\Scripts\activate.bat" (
    call "%REPO_DIR%venv\Scripts\activate.bat"
    echo [OK] Virtual environment activated.
) else (
    echo [INFO] No virtual environment found. Using system Python.
)

REM Check Streamlit
python -c "import streamlit" >nul 2>&1
if %errorlevel% neq 0 (
    echo [ERROR] Streamlit is not installed.
    echo [ERROR] Run: pip install streamlit
    echo [ERROR] See %LOG_FILE% for details.
    echo [%date% %time%] Streamlit not found > "%LOG_FILE%"
    pause
    exit /b 1
)

echo.
echo  ============================================
echo   MLB VIP MODEL - Starting Control Panel
echo  ============================================
echo.
echo  Browser will open automatically.
echo  Press Ctrl+C to stop.
echo.

REM Start Streamlit and open browser
python -m streamlit run src/control_panel.py --server.headless false --browser.gatherUsageStats false 2>> "%LOG_FILE%"

if %errorlevel% neq 0 (
    echo.
    echo [ERROR] Streamlit failed to start.
    echo [ERROR] Check if port 8501 is already in use.
    echo [ERROR] See %LOG_FILE% for details.
    echo [%date% %time%] Streamlit failed with code %errorlevel% >> "%LOG_FILE%"
    pause
)

endlocal
