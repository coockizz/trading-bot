@echo off
REM Lance le bot sous Windows.
REM   run.bat --simulate 500      -> simulation hors ligne
REM   run.bat                     -> dry-run sur le testnet

setlocal
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
    echo Environnement virtuel absent. Lance d'abord install.bat
    pause
    exit /b 1
)

REM Charge les variables du fichier .env
if exist ".env" (
    for /f "usebackq tokens=1,* delims==" %%a in (".env") do (
        if not "%%a"=="" if not "%%a:~0,1%"=="#" set "%%a=%%b"
    )
)

echo %* | findstr /c:"--config" >nul
if errorlevel 1 (
    .venv\Scripts\python.exe -m src.main --config config/testnet.yaml %*
) else (
    .venv\Scripts\python.exe -m src.main %*
)
