#!/bin/bash
set -e
echo "=== Kimmy Trading Bot — Server Setup ==="

BOT_DIR="/root/trading-agent"
VENV="/root/venv"
SERVICE="kimmy"

# 1. Make sure we're in the right place
cd "$BOT_DIR"

# 2. Install systemd service
echo "[1/5] Installing systemd service..."
cp "$BOT_DIR/kimmy.service" /etc/systemd/system/kimmy.service
systemctl daemon-reload
systemctl enable "$SERVICE"
echo "      Service enabled (will auto-start on every reboot)"

# 3. Set up log rotation so logs don't fill the disk
echo "[2/5] Configuring log rotation..."
cat > /etc/logrotate.d/kimmy << 'EOF'
/var/log/journal/kimmy.log {
    daily
    rotate 14
    compress
    missingok
    notifempty
}
EOF

# 4. Set server timezone to ET so scheduler fires at correct times
echo "[3/5] Setting timezone to America/New_York..."
timedatectl set-timezone America/New_York
echo "      Timezone: $(timedatectl show --property=Timezone --value)"

# 5. Start the service
echo "[4/5] Starting bot service..."
systemctl start "$SERVICE"
sleep 3
systemctl status "$SERVICE" --no-pager -l

echo ""
echo "[5/5] Setup complete."
echo ""
echo "Useful commands:"
echo "  journalctl -u kimmy -f          # live logs"
echo "  systemctl status kimmy          # is it running?"
echo "  systemctl restart kimmy         # restart bot"
echo "  systemctl stop kimmy            # stop bot"
echo ""
echo "NOTE: Run monthly research once to populate the cache:"
echo "  systemctl stop kimmy"
echo "  cd /root/trading-agent && ~/venv/bin/python3 main.py --monthly"
echo "  systemctl start kimmy"
