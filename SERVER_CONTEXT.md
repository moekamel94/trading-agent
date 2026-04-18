# Server Fix Context

## Problem
`kimmy.service` is crash-looping on the DigitalOcean server (174.138.46.158).
It has failed 1,055+ times because the service file runs:

```
/opt/trading-agent/venv/bin/python -u main.py --discord
```

But `main.py` does NOT have a `--discord` argument. It only accepts `--schedule` and `--dry-run`.

## Fix Required
Edit `/etc/systemd/system/kimmy.service` and change `--discord` to `--schedule`, then reload and restart.

Run this single command:
```bash
sed -i 's/main.py --discord/main.py --schedule/' /etc/systemd/system/kimmy.service && systemctl daemon-reload && systemctl restart kimmy.service && systemctl status kimmy.service
```

## Expected Result
Service should show `active (running)` and stay running without crash-looping.

## Project Location on Server
- Project: `/opt/trading-agent/`
- Venv: `/opt/trading-agent/venv/`
- Service file: `/etc/systemd/system/kimmy.service`
- Main entry: `/opt/trading-agent/main.py`

## What the Bot Does
- Runs a trading cycle 7x/day (Mon-Fri ET): 9:00, 9:35, 10:00, 11:30, 13:00, 14:30, 15:45
- Sends Discord notifications when trades fire
- Uses Alpaca paper trading API
- Claude AI makes BUY/SELL/HOLD decisions

## After Fixing the Service
1. Confirm service is running: `systemctl status kimmy.service`
2. Watch live logs: `journalctl -u kimmy.service -f`
3. Check the bot is not crash-looping anymore
