#!/bin/bash
# Auto-restart wrapper for the trading bot
cd "$(dirname "$0")"

while true; do
    echo "[$(date)] Starting bot..."
    ~/venv/bin/python3 main.py --discord
    echo "[$(date)] Bot exited (code $?). Restarting in 15 seconds..."
    sleep 15
done
