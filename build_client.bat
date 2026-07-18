@echo off
rem ============================================================
rem  Builds RivenTracker.exe from client.py using PyInstaller.
rem  Run this on Windows, from this folder. Output: dist\RivenTracker.exe
rem  Before building: set DEFAULT_REPO in client.py to your repo
rem  (e.g. "yourname/riven-tracker") so friends never have to type it.
rem ============================================================
py -m pip install --upgrade pyinstaller openpyxl
py -m PyInstaller --onefile --windowed --name RivenTracker ^
    --hidden-import openpyxl ^
    client.py
echo.
echo Done. Share:  dist\RivenTracker.exe
pause
