@echo off
setlocal
rem ======================================================================
rem  NBA LIVE 19 ONLINE - server (Blaze + match server)
rem  IP = the IP the players use to reach this PC:
rem       - with a VPN (Radmin VPN, ZeroTier, Tailscale...): your VPN IP;
rem       - over the internet with open ports: your public IP;
rem       - on the same home network: your local IP (e.g. 192.168.1.10).
rem  It is the same IP the players put in SERVER= in their launcher.
rem ======================================================================
set "IP=0.0.0.0"
rem ======================================================================

cd /d "%~dp0"
if "%IP%"=="0.0.0.0" (
    echo Open this file with Notepad and put your IP in IP=
    pause
    exit /b 1
)
where python >nul 2>nul
if errorlevel 1 (
    echo Python 3.10 or newer is required: https://www.python.org/downloads/
    echo When installing it, tick "Add python.exe to PATH".
    pause
    exit /b 1
)
echo NBA LIVE 19 server on %IP%. Keep this window open while people play.
echo Logs go to the logs folder. Close this window to stop the server.
python -u servidor.py %IP%
pause
