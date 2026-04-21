@echo off
title Kimmy Trading Bot
cd /d "%~dp0"

:loop
echo [%date% %time%] Starting bot...
python main.py --discord
echo [%date% %time%] Bot stopped (exit code %errorlevel%). Restarting in 15 seconds...
timeout /t 15 /nobreak >nul
goto loop
