@echo off
REM Installation du bot de trading sous Windows.
REM Double-clique sur ce fichier, ou lance-le depuis PowerShell : .\install.bat

setlocal enabledelayedexpansion
cd /d "%~dp0"

echo.
echo === Installation du bot de trading ===
echo.

REM --- 1. Python -----------------------------------------------------------
REM Le script n'installe pas Python : telecharge-le depuis python.org si besoin.
where python >nul 2>&1
if errorlevel 1 (
    echo ERREUR: Python est introuvable.
    echo.
    echo   Telecharge Python 3.11 ou plus recent sur https://www.python.org/downloads/
    echo   IMPORTANT: coche "Add Python to PATH" pendant l'installation.
    echo   Puis relance ce fichier.
    pause
    exit /b 1
)

python -c "import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)"
if errorlevel 1 (
    echo ERREUR: ta version de Python est trop ancienne ^(3.11 minimum requis^).
    python --version
    echo   Telecharge une version recente sur https://www.python.org/downloads/
    pause
    exit /b 1
)
for /f "delims=" %%v in ('python --version') do echo Python trouve: %%v

REM --- 2. Environnement virtuel -------------------------------------------
if exist ".venv\Scripts\python.exe" (
    echo Environnement virtuel deja present, reutilise.
) else (
    echo Creation de l'environnement virtuel...
    python -m venv .venv
    if errorlevel 1 goto :echec
)

REM --- 3. Dependances ------------------------------------------------------
echo Installation des dependances...
.venv\Scripts\python.exe -m pip install --upgrade pip --quiet
.venv\Scripts\python.exe -m pip install -r requirements.txt --quiet
if errorlevel 1 goto :echec
.venv\Scripts\python.exe -m pip install -r requirements-dev.txt --quiet

REM --- 4. Fichier .env -----------------------------------------------------
if exist ".env" (
    echo Fichier .env deja present, conserve.
) else (
    copy /y .env.example .env >nul
    echo Fichier .env cree. Ouvre-le et colle tes cles API.
)

REM --- 5. Dossiers de travail ---------------------------------------------
if not exist "logs" mkdir logs
if not exist "state" mkdir state

REM --- 6. Verification -----------------------------------------------------
echo Verification en simulation...
.venv\Scripts\python.exe -m src.main --config config/testnet.yaml --simulate 200 >nul 2>&1
if errorlevel 1 (
    echo ATTENTION: la simulation a echoue. Lance la commande suivante pour voir l'erreur:
    echo     .venv\Scripts\python.exe -m src.main --config config/testnet.yaml --simulate 200
) else (
    echo Simulation: OK
)

echo.
echo === Installation terminee ===
echo.
echo   Pour lancer le bot en simulation (sans cle API):
echo       run.bat --simulate 500
echo.
echo   Lis GUIDE_DEBUTANT.md avant d'aller plus loin.
echo.
pause
exit /b 0

:echec
echo.
echo ERREUR pendant l'installation. Relis les messages ci-dessus.
pause
exit /b 1
