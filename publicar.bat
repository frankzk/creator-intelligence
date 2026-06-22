@echo off
REM ---------------------------------------------------------------
REM  Publicar la Fabrica en internet (tunel Cloudflare) - un clic
REM  Te da una URL https para entrar desde el celular o la laptop.
REM
REM  USA SOLO ESTE ARCHIVO (no abras la app por separado):
REM  doble clic aqui arranca la app Y abre el tunel.
REM
REM  Una sola vez, antes de la primera vez:
REM   1) Pon una contrasena en APP_PASSWORD dentro del archivo .env
REM   2) Instala cloudflared:  winget install --id Cloudflare.cloudflared
REM ---------------------------------------------------------------
setlocal
cd /d "%~dp0"
title Publicar Fabrica de creativos

echo.
echo  ===============================================================
echo    PUBLICAR LA FABRICA EN INTERNET  (tunel Cloudflare)
echo  ===============================================================
echo.

REM --- Buscar cloudflared: PATH, o la ruta de winget (sirve sin reiniciar) ---
set "CF="
where cloudflared >nul 2>nul && set "CF=cloudflared"
if not defined CF if exist "%LOCALAPPDATA%\Microsoft\WinGet\Links\cloudflared.exe" set "CF=%LOCALAPPDATA%\Microsoft\WinGet\Links\cloudflared.exe"
if not defined CF if exist "%LOCALAPPDATA%\Microsoft\WindowsApps\cloudflared.exe" set "CF=%LOCALAPPDATA%\Microsoft\WindowsApps\cloudflared.exe"
if not defined CF goto nocf

echo  RECORDATORIO: pon una APP_PASSWORD en el archivo .env
echo  para que nadie mas entre con el link.
echo.
echo  Iniciando la app en otra ventana... no la cierres.
start "Fabrica - NO CERRAR" cmd /k python main.py

echo  Esperando a que la app levante...
timeout /t 6 >nul

echo.
echo  ---------------------------------------------------------------
echo   En unos segundos aparecera una linea como:
echo        https://algo-al-azar.trycloudflare.com
echo   COPIA esa direccion y abrela en tu celular o laptop.
echo   Te pedira usuario y contrasena: los de tu archivo .env
echo   Para APAGAR: cierra esta ventana y la de la app.
echo  ---------------------------------------------------------------
echo.
"%CF%" tunnel --url http://localhost:8000

echo.
echo  El tunel se cerro. Cierra tambien la ventana de la app.
pause
exit /b

:nocf
echo  [!] No encuentro cloudflared.
echo.
echo  Instalalo UNA sola vez en PowerShell con:
echo      winget install --id Cloudflare.cloudflared
echo.
echo  Si lo ACABAS de instalar, REINICIA la computadora
echo  y vuelve a dar doble clic aqui.
echo.
pause
exit /b
