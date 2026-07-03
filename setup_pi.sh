#!/bin/bash
# setup_pi.sh - Set up /home/pi/home_automation as a git-managed home automation system
# Run on the Pi: bash setup_pi.sh
#
# Make executable first: chmod +x setup_pi.sh

set -e

PROJECT_DIR="/home/pi/home_automation"
BACKUP_DIR="/tmp/home_automation_backup_$(date +%Y%m%d_%H%M%S)"
SERVICE_NAME="home_automation.service"
LOG_DIR="/var/log/home_automation"

echo "=== Home Automation Pi Setup ==="
echo ""

# Step 1: Stop the daemon if running
echo "Step 1: Stopping existing daemon..."
if systemctl is-active --quiet "$SERVICE_NAME" 2>/dev/null; then
    echo "  Stopping $SERVICE_NAME..."
    sudo systemctl stop "$SERVICE_NAME"
    echo "  Stopped."
else
    echo "  Daemon not running (skipping)."
fi
echo ""

# Step 2: Backup current data (using /tmp so it auto-cleans on reboot)
echo "Step 2: Backing up current data to $BACKUP_DIR ..."
if [ -d "$PROJECT_DIR" ]; then
    mkdir -p "$BACKUP_DIR"
    cp -a "$PROJECT_DIR"/* "$PROJECT_DIR"/.* "$BACKUP_DIR"/ 2>/dev/null || true
    echo "  Backup saved to: $BACKUP_DIR"
else
    echo "  No existing directory found (fresh install, skipping backup)."
fi
echo ""

# Step 3: Confirm before proceeding
read -p "Step 3: Proceed with setup? [y/N] " confirm
if [ "$confirm" != "y" ] && [ "$confirm" != "Y" ]; then
    echo "Aborted. Nothing was changed."
    exit 1
fi

# Step 4: Clone or update the git repo
echo "Step 4: Setting up git repository..."
if [ -d "$PROJECT_DIR" ]; then
    cd "$PROJECT_DIR"
    if [ -d ".git" ]; then
        echo "  Git repo exists, pulling latest..."
        git pull origin main
    else
        echo "  Git repo exists but .git missing, recreating..."
        rm -rf "$PROJECT_DIR"
        git clone git@github.com:IainBate/home-automation.git "$PROJECT_DIR"
    fi
else
    git clone git@github.com:IainBate/home-automation.git "$PROJECT_DIR"
fi
cd "$PROJECT_DIR"
echo "  Repository ready."
echo ""

# Step 5: Set up virtual environment
echo "Step 5: Setting up virtual environment..."
if [ -d "venv" ]; then
    echo "  venv already exists, skipping creation."
else
    echo "  Creating venv..."
    python3 -m venv venv
    echo "  venv created."
fi

echo "  Installing dependencies..."
source venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt
echo "  Dependencies installed."
echo ""

# Step 6: Restore config files from backup if needed
echo "Step 6: Checking config files..."
if [ -f "config.yaml" ]; then
    echo "  config.yaml exists."
else
    echo "  WARNING: config.yaml not found in git!"
    if [ -d "$BACKUP_DIR" ] && [ -f "$BACKUP_DIR/config.yaml" ]; then
        echo "  Restoring from backup..."
        cp "$BACKUP_DIR/config.yaml" .
        echo "  Restored."
    else
        echo "  No backup found. You will need to create config.yaml manually."
    fi
fi

if [ -f "battery_mode_daemon_config.json" ]; then
    echo "  battery_mode_daemon_config.json exists."
else
    echo "  WARNING: battery_mode_daemon_config.json not found in git!"
    if [ -d "$BACKUP_DIR" ] && [ -f "$BACKUP_DIR/battery_mode_daemon_config.json" ]; then
        echo "  Restoring from backup..."
        cp "$BACKUP_DIR/battery_mode_daemon_config.json" .
        echo "  Restored."
    else
        echo "  No backup found. You will need to create this file manually."
    fi
fi

# Restore other runtime files from backup
if [ -d "$BACKUP_DIR" ]; then
    for f in config/solax_mode_change_log.json data/battery_mode_daemon_log.json; do
        if [ -f "$BACKUP_DIR/$f" ]; then
            mkdir -p "$(dirname "$f")"
            cp "$BACKUP_DIR/$f" "$f"
            echo "  Restored $f from backup."
        fi
    done
fi
echo ""

# Step 7: Create logs directory and symlink
echo "Step 7: Setting up logs..."
if [ ! -d "$LOG_DIR" ]; then
    echo "  Creating $LOG_DIR ..."
    sudo mkdir -p "$LOG_DIR"
    sudo chown pi:pi "$LOG_DIR"
fi

if [ -L "logs" ]; then
    echo "  logs symlink already exists."
elif [ -d "logs" ]; then
    echo "  logs directory exists (not a symlink), converting to symlink..."
    rm -rf logs
    ln -s "$LOG_DIR" logs
    echo "  Converted to symlink."
else
    echo "  Creating logs -> $LOG_DIR symlink..."
    ln -s "$LOG_DIR" logs
    echo "  Symlink created."
fi
echo ""

# Step 8: Install and enable systemd service
echo "Step 8: Installing systemd service..."
sudo cp scripts/home_automation.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable "$SERVICE_NAME"
sudo systemctl start "$SERVICE_NAME"
echo "  Service installed, enabled, and started."
echo ""

# Done
echo "=== Setup complete ==="
echo ""
echo "Verify:"
echo "  systemctl status $SERVICE_NAME"
echo "  journalctl -u $SERVICE_NAME -f"
echo ""
if [ -d "$BACKUP_DIR" ]; then
    echo "Backup available at: $BACKUP_DIR (will be cleaned on reboot)"
fi
