#!/bin/bash
# StockBot — Oracle Cloud / Linux setup script
# Run once after cloning the repo: bash setup.sh

set -e
echo "=============================="
echo " StockBot Setup"
echo "=============================="

# 1. System packages
sudo apt-get update -qq
sudo apt-get install -y python3 python3-pip python3-venv git

# 2. Create virtual environment
python3 -m venv venv
source venv/bin/activate

# 3. Install Python packages
pip install --upgrade pip
pip install -r linux/requirements.txt

# 4. Copy Linux db files (replace Windows SQL Server versions)
cp linux/db.py db.py
cp linux/dashboard.py dashboard.py
cp linux/clean_db.py clean_db.py

# 5. Init SQLite database
python clean_db.py

# 6. Open firewall port for dashboard (Oracle Cloud)
#    You also need to open port 8050 in the OCI Security List
sudo iptables -I INPUT -p tcp --dport 8050 -j ACCEPT
sudo iptables -I INPUT -p tcp --dport 8443 -j ACCEPT

echo ""
echo "=============================="
echo " Setup complete!"
echo "=============================="
echo ""
echo "Start the bot:"
echo "  source venv/bin/activate"
echo "  python stock_scanner.py"
echo ""
echo "Start the dashboard (separate terminal):"
echo "  source venv/bin/activate"
echo "  python dashboard.py"
echo ""
echo "Run as services (stays alive after logout):"
echo "  See README or run: bash linux/install_services.sh"
