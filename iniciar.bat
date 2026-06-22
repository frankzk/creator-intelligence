@echo off
REM ---------------------------------------------------------------
REM  Iniciar la Fabrica (solo la app).
REM  Usa este archivo cuando YA tengas la URL fija configurada
REM  (ver PUBLICAR-FIJO.md). El tunel corre solo como servicio.
REM
REM  Si todavia no tienes URL fija, usa publicar.bat (tunel rapido).
REM ---------------------------------------------------------------
setlocal
cd /d "%~dp0"
title Fabrica de creativos
echo.
echo  Iniciando la app...
echo  Abre tu URL fija (ej. https://fabrica.tudominio.com) en el celular.
echo  Para apagar: cierra esta ventana.
echo.
python main.py
echo.
echo  La app se detuvo.
pause
