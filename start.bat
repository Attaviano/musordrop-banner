@echo off
chcp 65001 >nul
cd /d "%~dp0"
where python >nul 2>nul || (
    echo Не найден Python. Установи Python 3.10 или новее: https://www.python.org/downloads/
    echo При установке отметь галочку "Add python.exe to PATH".
    pause
    exit /b
)
python -c "import flask, imageio_ffmpeg" 2>nul || (
    echo Устанавливаю зависимости...
    python -m pip install -r requirements.txt
)
python app.py
pause
