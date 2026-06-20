@echo off
REM ─────────────────────────────────────────────────────────────
REM  Respaldo de un clic — Fabrica de creativos
REM  Copia lo IRREEMPLAZABLE: base de datos, fotos y grabaciones.
REM  Recomendado: cierra la app (Ctrl+C) antes de correr esto.
REM  Doble clic en este archivo. Luego copia la carpeta a Drive/USB.
REM ─────────────────────────────────────────────────────────────
setlocal
cd /d "%~dp0"

for /f %%i in ('powershell -NoProfile -Command "Get-Date -Format yyyy-MM-dd_HHmm"') do set STAMP=%%i
set DEST=backups\backup_%STAMP%

echo.
echo  Creando respaldo en: %DEST%
echo.
mkdir "%DEST%" 2>nul

REM Base de datos (registro completo: campanas, personas, guiones, metricas)
copy /Y database.db      "%DEST%\" >nul 2>nul
copy /Y database.db-wal  "%DEST%\" >nul 2>nul
copy /Y database.db-shm  "%DEST%\" >nul 2>nul

REM Fotos de producto
if exist uploads robocopy uploads "%DEST%\uploads" /E /NFL /NDL /NJH /NJS /NC /NS /NP >nul

REM Grabaciones originales (lo mas valioso: tus modulos crudos)
if exist factory\src robocopy factory\src "%DEST%\factory_src" /E /NFL /NDL /NJH /NJS /NC /NS /NP >nul

echo  Respaldo LISTO en: %DEST%
echo.
echo  IMPORTANTE: copia esa carpeta a Google Drive o a un USB.
echo  (El codigo ya esta a salvo en GitHub; esto respalda tus datos.)
echo.
pause
