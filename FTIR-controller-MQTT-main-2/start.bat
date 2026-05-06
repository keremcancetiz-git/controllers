@echo off
cd /d %~dp0
call .venv\Scripts\activate
title MQTT Server
:loop
python -u main.py
echo Server exited at %date% %time%
echo Restarting in 5 seconds... (Ctrl+C to stop)
timeout /t 5
goto loop