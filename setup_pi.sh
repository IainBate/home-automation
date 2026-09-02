#!/bin/bash
# setup_pi.sh - Set up /home/pi/home_automation as a git-managed home automation system
# Run on the Pi: bash setup_pi.sh
#
# Make executable first: chmod +x setup_pi.sh

set -e

PROJECT_DIR="/home/pi/home_automation"
BACKUP_DIR="/tmp/home_automation_backup_$(date +%Y%m%d_%H%M%S)"
SERVICE_NAME="home_automation.service"
HOTWATER_SERVICE_NAME="home_automation_hotwater.service"
DASHBOARD_SERVICE_NAME="home_automation_dashboard.service"
LOG_DIR="/var/log/home_automation"

echo "=== Home Automation Pi Setup ==="
echo ""

# Step 1: Stop the daemons if running
echo "Step 1: Stopping existing daemons..."
if systemctl is-active --quiet "$SERVICE_NAME" 2>/dev/null; then
    echo "  Stopping $SERVICE_NAME..."
    sudo systemctl stop "$SERVICE_NAME"
    echo "  Stopped."
else
    echo "  $SERVICE_NAME not running (skipping)."
fi
if systemctl is-active --quiet "$HOTWATER_SERVICE_NAME" 2>/dev/null; then
    echo "  Stopping $HOTWATER_SERVICE_NAME..."
    sudo systemctl stop "$HOTWATER_SERVICE_NAME"
    echo "  Stopped."
else
    echo "  $HOTWATER_SERVICE_NAME not running (skipping)."
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
echo "  (scikit-learn/numpy can take a very long time to compile from source on a Pi -"
echo "   Raspberry Pi OS's pip is usually already configured for piwheels' prebuilt ARM"
echo "   wheels; if this step is unexpectedly slow, check 'pip config list' for piwheels.org)"
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

# secrets.yaml is gitignored by design (real credentials), but its encrypted
# backup (secrets.yaml.enc) IS tracked in git - see scripts/encrypt_secrets.sh.
if [ -f "secrets.yaml" ]; then
    echo "  secrets.yaml exists."
elif [ -d "$BACKUP_DIR" ] && [ -f "$BACKUP_DIR/secrets.yaml" ]; then
    echo "  Restoring secrets.yaml from local backup..."
    cp "$BACKUP_DIR/secrets.yaml" .
    echo "  Restored."
elif [ -f "secrets.yaml.enc" ]; then
    echo "  secrets.yaml not found, but secrets.yaml.enc (encrypted git backup) is present."
    echo "  Recovery hint: the passphrase is this Pi's own login password (unless a"
    echo "  different one was set - see scripts/decrypt_secrets.sh for details)."
    read -p "  Decrypt it now? [y/N] " decrypt_confirm
    if [ "$decrypt_confirm" = "y" ] || [ "$decrypt_confirm" = "Y" ]; then
        bash scripts/decrypt_secrets.sh
    else
        echo "  Skipped. Run 'bash scripts/decrypt_secrets.sh' later, or create secrets.yaml manually."
    fi
else
    echo "  WARNING: secrets.yaml not found and no secrets.yaml.enc backup in git!"
    echo "  You will need to create secrets.yaml manually (see secrets.yaml.example)."
fi

# Restore or create runtime data files from backup
if [ -d "$BACKUP_DIR" ]; then
    for f in config/solax_mode_change_log.json data/battery_mode_daemon_log.json; do
        if [ -f "$BACKUP_DIR/$f" ]; then
            mkdir -p "$(dirname "$f")"
            cp "$BACKUP_DIR/$f" "$f"
            echo "  Restored $f from backup."
        fi
    done
fi

# Create runtime data files if they don't exist (not tracked in git)
if [ ! -f "config/solax_mode_change_log.json" ]; then
    mkdir -p config
    echo '{"last_mode_change": null, "change_history": []}' > config/solax_mode_change_log.json
    echo "  Created config/solax_mode_change_log.json (empty)."
fi

if [ ! -f "data/battery_mode_daemon_log.json" ]; then
    mkdir -p data
    echo '{"last_change_timestamp": null, "last_change_mode": null, "last_change_reason": null, "change_history": []}' > data/battery_mode_daemon_log.json
    echo "  Created data/battery_mode_daemon_log.json (empty)."
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

# Step 9: Install and enable the hot water automation service - independent
# process from the battery daemon above (base_daemon.py's TwoTierPollingDaemon
# architecture, own state/log files), gated by config.yaml's
# hotwater_automation.enabled so it's safe to install even before that's
# turned on (the daemon checks the flag itself on every cycle).
echo "Step 9: Installing hot water automation service..."
sudo cp scripts/home_automation_hotwater.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable "$HOTWATER_SERVICE_NAME"
sudo systemctl start "$HOTWATER_SERVICE_NAME"
echo "  Hot water service installed, enabled, and started."
echo ""

# Step 10: Install and enable the status dashboard service - independent of
# both daemons above; read-only, never touches battery_mode_daemon.py or
# hotwater_mode_daemon.py's state, so it's always safe to run alongside them.
echo "Step 10: Installing dashboard service..."
sudo cp scripts/home_automation_dashboard.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable "$DASHBOARD_SERVICE_NAME"
sudo systemctl start "$DASHBOARD_SERVICE_NAME"
echo "  Dashboard service installed, enabled, and started."
echo ""

# Done
echo "=== Setup complete ==="
echo ""
echo "Verify:"
echo "  systemctl status $SERVICE_NAME"
echo "  journalctl -u $SERVICE_NAME -f"
echo "  systemctl status $HOTWATER_SERVICE_NAME"
echo "  journalctl -u $HOTWATER_SERVICE_NAME -f"
echo "  systemctl status $DASHBOARD_SERVICE_NAME"
echo "  journalctl -u $DASHBOARD_SERVICE_NAME -f"
echo "  Dashboard: http://<this Pi's IP>:8000/ from any device on your home WiFi"
echo ""
if [ -d "$BACKUP_DIR" ]; then
    echo "Backup available at: $BACKUP_DIR (will be cleaned on reboot)"
fi
echo ""
echo "Optional next steps (none of these run automatically):"
echo "  - Hot water automation: set hotwater_automation.enabled: true in config.yaml"
echo "      once melcloud (above) is configured and tested (this service runs"
echo "      regardless, but sits idle checking the flag until it's on)."
echo "  - Solar forecast: set location.latitude/longitude in config.yaml, then"
echo "      python3 scripts/solar_forecast_trainer.py && python3 scripts/solar_forecast_predictor.py"
echo "    then add cron entries per config.yaml's solar_forecast comments."
echo "  - Airstage: set airstage.ip_address/device_id in config.yaml (see its comments)."
echo "  - Resideo: set resideo.client_id/client_secret, then"
echo "      python3 scripts/resideo_oauth_setup.py"
echo "  - MG SAIC (EV battery/range): set mg_saic.username/password in secrets.yaml,"
echo "      then add an hourly cron entry per config.yaml's mg_saic comments"
echo "  - Claude usage: log in with \`claude\` on this Pi once, set claude_usage.enabled: true,"
echo "      then add a cron entry (10+ min) - no token copying needed, see config.yaml comments"
echo "  - Remote access without port-forwarding: consider Tailscale"
echo "      (curl -fsSL https://tailscale.com/install.sh | sh && sudo tailscale up)"
echo "    then browse to the dashboard using this Pi's Tailscale address instead of its LAN IP."
