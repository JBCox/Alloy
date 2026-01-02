@echo off
setlocal enabledelayedexpansion

:: ============================================================================
:: Alloy Executable Builder
:: Creates standalone .exe files using PyInstaller
:: Builds both GUI (windowed) and CLI (console) versions
:: ============================================================================

title Alloy Build

echo.
echo  ============================================
echo   Alloy Executable Builder
echo   Creates GUI and CLI executables
echo  ============================================
echo.

set "ALLOY_DIR=%~dp0"
set "ALLOY_DIR=%ALLOY_DIR:~0,-1%"

:: Check for Python
python --version >nul 2>&1
if %errorLevel% neq 0 (
    echo [ERROR] Python is not installed.
    pause
    exit /b 1
)

echo [1/4] Installing PyInstaller...
echo.
pip install pyinstaller -q
if %errorLevel% neq 0 (
    echo [ERROR] Failed to install PyInstaller.
    pause
    exit /b 1
)
echo        PyInstaller ready.

echo.
echo [2/4] Installing dependencies...
echo.
pip install -q -r "%ALLOY_DIR%\requirements.txt"
echo        Dependencies installed.

echo.
echo [3/4] Building executable...
echo.
echo        This may take a few minutes...
echo.

:: Build using spec file if it exists, otherwise use command line
if exist "%ALLOY_DIR%\alloy.spec" (
    pyinstaller --clean --noconfirm "%ALLOY_DIR%\alloy.spec"
) else (
    pyinstaller --clean --noconfirm ^
        --name "Alloy" ^
        --onedir ^
        --windowed ^
        --add-data "config.yaml;." ^
        --add-data "gui;gui" ^
        --hidden-import "tkinter" ^
        --hidden-import "tkinter.ttk" ^
        --hidden-import "rich" ^
        --hidden-import "prompt_toolkit" ^
        --hidden-import "yaml" ^
        --collect-all "rich" ^
        --collect-all "prompt_toolkit" ^
        "%ALLOY_DIR%\main.py"
)

if %errorLevel% neq 0 (
    echo.
    echo [ERROR] Build failed. Check the output above for errors.
    pause
    exit /b 1
)

echo.
echo [4/4] Build complete!
echo.

:: Check if output exists
if exist "%ALLOY_DIR%\dist\Alloy" (
    echo  ============================================
    echo   SUCCESS! Executable created:
    echo.
    echo   %ALLOY_DIR%\dist\Alloy\Alloy.exe
    echo.
    echo   To distribute:
    echo   1. Copy the entire 'dist\Alloy' folder
    echo   2. Users run Alloy.exe directly
    echo   3. No Python installation needed!
    echo  ============================================

    :: Open the dist folder
    explorer "%ALLOY_DIR%\dist\Alloy"
) else (
    echo [WARNING] Could not find output. Check dist folder manually.
)

echo.
pause
