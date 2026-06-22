@echo off
REM ─────────────────────────────────────────────────────────────
REM  Publicar la Fabrica en internet (tunel Cloudflare) — un clic
REM  Te da una URL https para entrar desde el celular o la laptop.
REM
REM  USA SOLO ESTE ARCHIVO (no abras la app por separado).
REM  Doble clic aqui: arranca la app Y abre el tunel.
REM
REM  Una sola vez, antes de la primera vez:
REM   1) Pon una contrasena en APP_PASSWORD dentro del archivo .env
REM   2) Instala cloudflared:  winget install --id Cloudflare.cloudflared
REM ─────────────────────────────────────────────────────────────
setlocal
cd /d "%~dp0"
title Publicar Fabrica de creativos

REM ── 1) Verifica cloudflared ──
where cloudflared >nul 2>nul
if errorlevel 1 (
  echo.
  echo  [!] No encuentro "cloudflared" instalado.
  echo      Instalalo UNA sola vez abriendo PowerShell y pegando:
  echo.
  echo         winget install --id Cloudflare.cloudflared
  echo.
  echo      (o descarga cloudflared.exe y dejalo en ESTA carpeta).
  echo      Luego vuelve a dar doble clic aqui.
  echo.
  pause
  exit /b
)

REM ── 2) Recordatorio de contrasena ──
echo.
echo  RECORDATORIO: la app debe tener una APP_PASSWORD en el archivo .env.
echo  Si esta vacia, cualquiera con el link podria entrar y gastar tus creditos.
echo.

REM ── 3) Arranca la app en otra ventana ──
echo  Iniciando la app... (se abre otra ventana; dejala abierta)
start "Fabrica - NO CERRAR" cmd /k "python main.py"

echo  Esperando a que la app levante...
timeout /t 6 >nul

REM ── 4) Abre el tunel publico ──
echo.
echo  ──────────────────────────────────────────────────────────────
echo   En unos segundos veras una linea como:
echo        https://algo-al-azar.trycloudflare.com
echo   COPIA esa direccion y abrela en tu celular o laptop.
echo   Te pedira usuario y contrasena (los de tu .env).
echo.
echo   Para APAGAR todo: cierra esta ventana y la otra (la de la app).
echo  ──────────────────────────────────────────────────────────────
echo.
cloudflared tunnel --url http://localhost:8000

echo.
echo  El tunel se cerro. Cierra tambien la ventana de la app.
pause
