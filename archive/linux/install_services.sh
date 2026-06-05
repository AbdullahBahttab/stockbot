#!/bin/bash
# Install systemd services so bot + dashboard survive reboots.
# Run from the project root: bash linux/install_services.sh

set -e
WORKDIR=$(pwd)
PYTHON="$WORKDIR/venv/bin/python"
USER=$(whoami)

echo "Installing systemd services..."
echo "  Working dir : $WORKDIR"
echo "  Python      : $PYTHON"
echo "  User        : $USER"

# ── Bot service ───────────────────────────────────────────────
sudo tee /etc/systemd/system/stockbot.service > /dev/null <<EOF
[Unit]
Description=StockBot Scanner
After=network.target

[Service]
Type=simple
User=$USER
WorkingDirectory=$WORKDIR
ExecStart=$PYTHON stock_scanner.py
Restart=always
RestartSec=10
StandardOutput=append:$WORKDIR/scanner.log
StandardError=append:$WORKDIR/bot_error.log
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=multi-user.target
EOF

# ── Dashboard service ─────────────────────────────────────────
sudo tee /etc/systemd/system/stockbot-dash.service > /dev/null <<EOF
[Unit]
Description=StockBot Dashboard
After=network.target

[Service]
Type=simple
User=$USER
WorkingDirectory=$WORKDIR
ExecStart=$PYTHON dashboard.py
Restart=always
RestartSec=10
StandardOutput=append:$WORKDIR/dash_error.log
StandardError=append:$WORKDIR/dash_error.log
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=multi-user.target
EOF

# ── Enable and start ──────────────────────────────────────────
sudo systemctl daemon-reload
sudo systemctl enable stockbot
sudo systemctl enable stockbot-dash
sudo systemctl start  stockbot
sudo systemctl start  stockbot-dash

echo ""
echo "Services installed and started!"
echo ""
echo "Useful commands:"
echo "  sudo systemctl status stockbot        — check bot status"
echo "  sudo systemctl status stockbot-dash   — check dashboard status"
echo "  sudo systemctl restart stockbot       — restart bot"
echo "  tail -f scanner.log                   — live bot logs"
