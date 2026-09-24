@echo off
chcp 65001 >nul
if "%~1"=="" (
    echo Перетащи одно или несколько видео на этот файл.
    pause
    exit /b
)
python "%~dp0musordrop.py" %*
pause
