@echo off
REM ============================================================
REM  Build PicoControl.exe from pico_gui.py
REM  Creates a clean virtual environment so the exe stays small.
REM  Just double-click this file, or run it from a terminal.
REM ============================================================
setlocal
cd /d "%~dp0"

REM --- pick the Python launcher ---
set "PY=py -3"
%PY% --version >nul 2>&1
if errorlevel 1 set "PY=python"
%PY% --version >nul 2>&1
if errorlevel 1 (
    echo [ERROR] Python not found on PATH. Install Python 3 and try again.
    pause
    exit /b 1
)

echo.
echo === Creating clean build venv (.buildenv) ===
if exist ".buildenv" rmdir /s /q ".buildenv"
%PY% -m venv .buildenv
if errorlevel 1 ( echo [ERROR] venv creation failed. & pause & exit /b 1 )

call ".buildenv\Scripts\activate.bat"

echo.
echo === Installing PyInstaller ===
python -m pip install --upgrade pip
python -m pip install pyinstaller
if errorlevel 1 ( echo [ERROR] pip install failed. & pause & exit /b 1 )

echo.
echo === Building PicoControl.exe ===
pyinstaller --onefile --noconsole --name PicoControl ^
    --clean --noconfirm ^
    pico_gui.py
if errorlevel 1 ( echo [ERROR] PyInstaller build failed. & pause & exit /b 1 )

echo.
echo ============================================================
echo  DONE.  Your executable is here:
echo     %cd%\dist\PicoControl.exe
echo ============================================================
echo.
pause
