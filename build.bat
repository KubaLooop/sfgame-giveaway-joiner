@echo off
cd /d "%~dp0"
where py >nul 2>nul && (set PY=py -3) || (set PY=python)
%PY% -m pip install --upgrade pip
%PY% -m pip install -r requirements.txt pyinstaller
%PY% -m PyInstaller --noconfirm --clean --onefile --windowed --name SfgGiveaway --add-data "data;data" --collect-data customtkinter run.py
echo.
echo Hotovo: dist\SfgGiveaway.exe
pause
