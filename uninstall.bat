@echo off
setlocal enabledelayedexpansion

:: ============================================================================
:: Alloy Uninstaller
:: Removes shortcuts and cleans up (keeps your config)
:: ============================================================================

title Alloy Uninstaller

echo.
echo  ============================================
echo   Alloy Uninstaller
echo  ============================================
echo.
echo  This will remove:
echo    - Desktop shortcut
echo    - Start Menu shortcut
echo    - Generated launcher scripts
echo.
echo  This will NOT remove:
echo    - Your config.yaml (keeps your settings)
echo    - The Alloy source code
echo    - Python or pip packages
echo.

set /p CONFIRM="Continue? (y/n): "
if /i not "%CONFIRM%"=="y" (
    echo Cancelled.
    pause
    exit /b 0
)

echo.
echo Removing shortcuts...

:: Remove Desktop shortcut
set "DESKTOP=%USERPROFILE%\Desktop"
if exist "%DESKTOP%\Alloy.lnk" (
    del "%DESKTOP%\Alloy.lnk"
    echo   Removed Desktop shortcut
)

:: Remove Start Menu shortcut
set "STARTMENU=%APPDATA%\Microsoft\Windows\Start Menu\Programs"
if exist "%STARTMENU%\Alloy.lnk" (
    del "%STARTMENU%\Alloy.lnk"
    echo   Removed Start Menu shortcut
)

:: Remove generated scripts
set "ALLOY_DIR=%~dp0"
if exist "%ALLOY_DIR%alloy-cli.bat" (
    del "%ALLOY_DIR%alloy-cli.bat"
    echo   Removed alloy-cli.bat
)
if exist "%ALLOY_DIR%alloy-gui.bat" (
    del "%ALLOY_DIR%alloy-gui.bat"
    echo   Removed alloy-gui.bat
)

:: Remove build artifacts
if exist "%ALLOY_DIR%dist" (
    echo.
    set /p REMOVE_DIST="Remove build folder (dist)? (y/n): "
    if /i "!REMOVE_DIST!"=="y" (
        rmdir /s /q "%ALLOY_DIR%dist"
        echo   Removed dist folder
    )
)
if exist "%ALLOY_DIR%build" (
    rmdir /s /q "%ALLOY_DIR%build"
    echo   Removed build folder
)

echo.
echo  ============================================
echo   Uninstall complete!
echo.
echo   Your config.yaml has been preserved.
echo   To fully remove Alloy, delete this folder.
echo  ============================================
echo.

pause
