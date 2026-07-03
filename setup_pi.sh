#!/bin/bash
# setup_pi.sh - Replace /home/pi/home_automation with a git clone from GitHub
# Run this on the Pi: bash setup_pi.sh
#
# Make it executable first: chmod +x setup_pi.sh

set -e

echo "=== Home Automation Pi Setup ==="
echo ""

# Step 1: Stop the daemon
echo "Step 1: Stopping the battery mode daemon..."
if pgrep -f battery_mode_daemon > /dev/null 2>&1; then
    echo "  Stopping daemon..."
    pkill -f battery_mode_daemon || true
    sleep 2
    echo "  Daemon stopped."
else
    echo "  Daemon not running (skipping)."
fi
echo ""

# Step 2: Backup current data
BACKUP_DIR="/home/pi/home_automation_backup_$(date +%Y%m%d_%H%M%S)"
echo "Step 2: Backing up current data to $BACKUP_DIR ..."
cp -a /home/pi/home_automation "$BACKUP_DIR"
echo "  Backup saved to: $BACKUP_DIR"
echo "  If anything goes wrong, you can restore from here."
echo ""

# Step 3: Confirm before deleting
read -p "Step 3: Delete the current /home/pi/home_automation folder? [y/N] " confirm
if [ "$confirm" != "y" ] && [ "$confirm" != "Y" ]; then
    echo "Aborted. Nothing was deleted."
    exit 1
fi
rm -rf /home/pi/home_automation
echo "  Deleted."
echo ""

# Step 4: Clone from GitHub
echo "Step 4: Cloning from GitHub..."
git clone git@github.com:IainBate/home-automation.git /home/pi/home_automation
cd /home/pi/home_automation
echo "  Clone complete."
echo ""

# Step 5: Verify files
echo "Step 5: Verifying files..."
echo "  Files in repo:"
ls -la
echo ""

# Step 6: Restore config if needed
echo "Step 6: Checking config files..."
if [ -f "config.yaml" ]; then
    echo "  config.yaml exists (from git)."
else
    echo "  WARNING: config.yaml not found in git!"
    echo "  If you had custom settings, restore from: $BACKUP_DIR/config.yaml"
fi

if [ -f "battery_mode_daemon_config.json" ]; then
    echo "  battery_mode_daemon_config.json exists (from git)."
else
    echo "  WARNING: battery_mode_daemon_config.json not found in git!"
    echo "  If you had custom settings, restore from: $BACKUP_DIR/battery_mode_daemon_config.json"
fi
echo ""

# Step 7: Restore virtual environment if needed
echo "Step 7: Virtual environment..."
if [ ! -d "myenv" ]; then
    echo "  myenv not found. Recreating..."
    python3 -m venv myenv
    source myenv/bin/activate
    pip install -r requirements.txt
    echo "  Virtual environment created."
else
    echo "  myenv already exists (skipping)."
fi
echo ""

# Done
echo "=== Setup complete ==="
echo ""
echo "Next steps:"
echo "  1. Verify your config.yaml has correct inverter IPs and Ohme credentials"
echo "  2. Verify battery_mode_daemon_config.json has your schedule"
echo "  3. Test: python3 scripts/solax_modbus_status_report.py"
echo "  4. Start daemon: python3 scripts/battery_mode_daemon.py battery_mode_daemon_config.json"
echo ""
echo "Backup available at: $BACKUP_DIR (if you need to restore anything)"
