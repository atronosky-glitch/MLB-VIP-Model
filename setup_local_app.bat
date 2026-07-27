@echo off
setlocal

REM ── MLB VIP Model First-Time Setup ──────────────────────────────
REM Run this once to set up the local application.

title MLB VIP Model - Setup

set "REPO_DIR=%~dp0"
cd /d "%REPO_DIR%"

echo.
echo  ============================================
echo   MLB VIP MODEL - First-Time Setup
echo  ============================================
echo.

REM Step 1: Check Python version
echo [1/8] Checking Python version...
python --version >nul 2>&1
if %errorlevel% neq 0 (
    echo [ERROR] Python is not installed or not in PATH.
    echo [ERROR] Install Python 3.10+ from https://python.org
    pause
    exit /b 1
)
python -c "import sys; v=sys.version_info; exit(0 if v.major==3 and v.minor>=10 else 1)" >nul 2>&1
if %errorlevel% neq 0 (
    echo [ERROR] Python 3.10 or higher is required.
    python --version
    pause
    exit /b 1
)
python --version
echo [OK] Python version is compatible.

REM Step 2: Create virtual environment
echo.
echo [2/8] Setting up virtual environment...
if not exist "venv" (
    python -m venv venv
    echo [OK] Virtual environment created.
) else (
    echo [OK] Virtual environment already exists.
)

REM Activate venv
call "venv\Scripts\activate.bat"

REM Step 3: Install dependencies
echo.
echo [3/8] Installing dependencies...
pip install -r requirements.txt --quiet 2>nul
if %errorlevel% neq 0 (
    echo [WARNING] Some dependencies may not have installed correctly.
    echo [WARNING] Try: pip install -r requirements.txt
) else (
    echo [OK] Dependencies installed.
)

REM Step 4: Verify Streamlit
echo.
echo [4/8] Verifying Streamlit...
python -c "import streamlit; print(f'Streamlit {streamlit.__version__}')" >nul 2>&1
if %errorlevel% neq 0 (
    echo [ERROR] Streamlit installation failed.
    echo [ERROR] Run: pip install streamlit
    pause
    exit /b 1
)
python -c "import streamlit; print(f'Streamlit {streamlit.__version__}')"
echo [OK] Streamlit verified.

REM Step 5: Create directories
echo.
echo [5/8] Creating directories...
if not exist "database" mkdir database
if not exist "data" mkdir data
if not exist "data\_api_cache" mkdir data\_api_cache
if not exist "output" mkdir output
if not exist "output\backups" mkdir output\backups
if not exist "logs" mkdir logs
echo [OK] Directories ready.

REM Step 6: Copy .env if missing
echo.
echo [6/8] Checking configuration...
if not exist ".env" (
    if exist ".env.example" (
        copy ".env.example" ".env" >nul
        echo [OK] Created .env from .env.example.
        echo [ACTION REQUIRED] Edit .env and add your SPORTSODDS_API_KEY.
    ) else (
        echo [INFO] No .env found. Create .env with your API key.
    )
) else (
    echo [OK] .env already exists.
)

REM Step 7: Validate configuration
echo.
echo [7/8] Validating configuration...
python -c "from src.production_config import load_config; c = load_config(); print('Config: OK' if c.api_key else 'Config: API key not set - edit .env')" 2>nul
if %errorlevel% neq 0 (
    echo [WARNING] Configuration validation could not run.
    echo [WARNING] Ensure .env has SPORTSODDS_API_KEY set.
)

REM Step 8: Run smoke test
echo.
echo [8/8] Running smoke test...
python -c "from src.production_config import load_config; from src.shadow_mode import load_shadow_config; from src.health_check import run_health_checks; print('Smoke test: OK')" 2>nul
if %errorlevel% neq 0 (
    echo [WARNING] Smoke test encountered issues.
    echo [WARNING] Check that all source files are intact.
) else (
    echo [OK] Smoke test passed.
)

echo.
echo  ============================================
echo   Setup Complete!
echo  ============================================
echo.
echo  NEXT STEPS:
echo  1. Edit .env and add your SPORTSODDS_API_KEY
echo  2. Double-click launch_mlb_model.bat to start
echo  3. Click RUN TODAY'S MLB MODEL
echo.

REM Step 9: Optionally create desktop shortcut
set /p CREATE_SHORTCUT="Create desktop shortcut? (y/n): "
if /i "%CREATE_SHORTCUT%"=="y" (
    powershell -ExecutionPolicy Bypass -File "%REPO_DIR%create_desktop_shortcut.ps1" 2>nul
    if %errorlevel% equ 0 (
        echo [OK] Desktop shortcut created: "MLB VIP Model"
    ) else (
        echo [INFO] Shortcut creation skipped.
    )
)

echo.
echo Press any key to exit...
pause >nul

endlocal
