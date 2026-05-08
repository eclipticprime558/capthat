@echo off
echo [CaptThat] Building...

REM Generate icon file
python -c "from capthat import save_icon_file; save_icon_file('icon.ico')"
if errorlevel 1 (
    echo [!] Icon generation failed. Make sure dependencies are installed.
    pause & exit /b 1
)

REM Install PyInstaller if needed
pip show pyinstaller >nul 2>&1 || pip install pyinstaller

REM Build single-file exe
pyinstaller --onefile --windowed --icon=icon.ico --name=CaptThat --clean capthat.py

if errorlevel 1 (
    echo [!] Build failed.
    pause & exit /b 1
)

echo.
echo [CaptThat] Build complete: dist\CaptThat.exe
echo Drop CaptThat.exe anywhere and run it — no installation required.
pause
