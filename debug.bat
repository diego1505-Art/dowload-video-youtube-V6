@echo off
title DowFlow DEBUG
cd /d "%~dp0"

echo === Dossier courant ===
cd

echo.
echo === Python ===
python --version

echo.
echo === Activation venv ===
if exist ".venv\Scripts\activate.bat" (
    call .venv\Scripts\activate.bat
    echo OK
) else (
    echo VENV ABSENT
)

echo.
echo === Modules ===
python -c "import flask; print('flask OK')"
python -c "import yt_dlp; print('yt_dlp OK')"

echo.
echo === Test import app.py ===
python -c "import app; print('app.py OK')"

echo.
echo === Lancement ===
python app.py

pause
