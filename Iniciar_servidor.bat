@echo off
setlocal
rem ======================================================================
rem  NBA LIVE 19 ONLINE - servidor (Blaze + servidor de juego)
rem  IP = la IP con la que los jugadores llegan a este PC:
rem       - con VPN (Radmin VPN, ZeroTier, Tailscale...): tu IP de la VPN;
rem       - por internet con puertos abiertos: tu IP publica;
rem       - en la misma red de casa: tu IP local (p. ej. 192.168.1.10).
rem  Es la misma IP que los jugadores ponen en SERVIDOR= de su lanzador.
rem ======================================================================
set "IP=0.0.0.0"
rem ======================================================================

cd /d "%~dp0"
if "%IP%"=="0.0.0.0" (
    echo Edita este archivo con el Bloc de notas y pon tu IP en IP=
    pause
    exit /b 1
)
where python >nul 2>nul
if errorlevel 1 (
    echo Hace falta Python 3.10 o mas nuevo: https://www.python.org/downloads/
    echo Al instalarlo marca "Add python.exe to PATH".
    pause
    exit /b 1
)
echo Servidor NBA LIVE 19 en %IP%. Deja esta ventana abierta mientras jueguen.
echo Registro en la carpeta logs. Para pararlo cierra esta ventana.
python -u servidor.py %IP%
pause
