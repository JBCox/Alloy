@echo off
setlocal enabledelayedexpansion

:: ============================================================================
:: Alloy Installer
:: One-click setup for Alloy - Multiple AIs, stronger together
:: ============================================================================

title Alloy Installer

echo.
echo  ============================================
echo   Alloy Installer
echo   Multiple AIs, stronger together
echo  ============================================
echo.

:: Check for administrator privileges (needed for some operations)
net session >nul 2>&1
if %errorLevel% neq 0 (
    echo [INFO] Running without admin rights. Some features may be limited.
    echo.
)

:: Get the directory where this script is located
set "ALLOY_DIR=%~dp0"
set "ALLOY_DIR=%ALLOY_DIR:~0,-1%"

echo [1/5] Checking Python installation...
echo.

:: Check if Python is installed
python --version >nul 2>&1
if %errorLevel% neq 0 (
    echo [ERROR] Python is not installed or not in PATH.
    echo.
    echo Please install Python 3.10+ from https://python.org
    echo Make sure to check "Add Python to PATH" during installation.
    echo.
    pause
    exit /b 1
)

:: Get Python version
for /f "tokens=2" %%i in ('python --version 2^>^&1') do set PYTHON_VERSION=%%i
echo        Found Python %PYTHON_VERSION%

:: Check Python version is 3.10+
for /f "tokens=1,2 delims=." %%a in ("%PYTHON_VERSION%") do (
    set MAJOR=%%a
    set MINOR=%%b
)
if %MAJOR% lss 3 (
    echo [ERROR] Python 3.10+ required. Found %PYTHON_VERSION%
    pause
    exit /b 1
)
if %MAJOR% equ 3 if %MINOR% lss 10 (
    echo [ERROR] Python 3.10+ required. Found %PYTHON_VERSION%
    pause
    exit /b 1
)

echo.
echo [2/5] Installing dependencies...
echo.

:: Install requirements
pip install -q -r "%ALLOY_DIR%\requirements.txt"
if %errorLevel% neq 0 (
    echo [ERROR] Failed to install dependencies.
    pause
    exit /b 1
)
echo        Dependencies installed successfully.

echo.
echo [3/5] Creating launcher scripts...
echo.

:: Create alloy.cmd in the Alloy directory (already have alloy.bat)
:: Create a CLI launcher
echo @echo off > "%ALLOY_DIR%\alloy-cli.bat"
echo python "%ALLOY_DIR%\main.py" %%* >> "%ALLOY_DIR%\alloy-cli.bat"
echo        Created alloy-cli.bat

:: Create a GUI launcher
echo @echo off > "%ALLOY_DIR%\alloy-gui.bat"
echo python "%ALLOY_DIR%\main.py" --gui >> "%ALLOY_DIR%\alloy-gui.bat"
echo        Created alloy-gui.bat

echo.
echo [4/5] Creating shortcuts...
echo.

:: Create Desktop shortcut for GUI
set "DESKTOP=%USERPROFILE%\Desktop"
set "SHORTCUT=%DESKTOP%\Alloy.lnk"

:: Use PowerShell to create shortcut
powershell -Command "$ws = New-Object -ComObject WScript.Shell; $s = $ws.CreateShortcut('%SHORTCUT%'); $s.TargetPath = 'pythonw'; $s.Arguments = '\"%ALLOY_DIR%\main.py\" --gui'; $s.WorkingDirectory = '%ALLOY_DIR%'; $s.Description = 'Alloy - Multiple AIs, stronger together'; $s.Save()" 2>nul

if exist "%SHORTCUT%" (
    echo        Created Desktop shortcut: Alloy.lnk
) else (
    echo        [SKIP] Could not create Desktop shortcut
)

:: Create Start Menu shortcut
set "STARTMENU=%APPDATA%\Microsoft\Windows\Start Menu\Programs"
set "STARTMENU_SHORTCUT=%STARTMENU%\Alloy.lnk"

powershell -Command "$ws = New-Object -ComObject WScript.Shell; $s = $ws.CreateShortcut('%STARTMENU_SHORTCUT%'); $s.TargetPath = 'pythonw'; $s.Arguments = '\"%ALLOY_DIR%\main.py\" --gui'; $s.WorkingDirectory = '%ALLOY_DIR%'; $s.Description = 'Alloy - Multiple AIs, stronger together'; $s.Save()" 2>nul

if exist "%STARTMENU_SHORTCUT%" (
    echo        Created Start Menu shortcut
) else (
    echo        [SKIP] Could not create Start Menu shortcut
)

echo.
echo [5/5] Verifying installation...
echo.

:: Quick verification
python -c "from gui.app import AlloyGUI; from main import AICollab; print('        All modules loaded successfully.')"
if %errorLevel% neq 0 (
    echo [WARNING] Some modules failed to load. Check the error above.
)

echo.
echo  ============================================
echo   Installation Complete!
echo  ============================================
echo.
echo  You can now run Alloy:
echo.
echo    GUI Mode:
echo      - Double-click the Desktop shortcut
echo      - Or run: alloy-gui.bat
echo      - Or run: python main.py --gui
echo.
echo    CLI Mode:
echo      - Run: alloy.bat
echo      - Or run: python main.py
echo.
echo  First time? Run the setup wizard:
echo      python main.py --setup
echo.
echo  ============================================
echo.

pause
