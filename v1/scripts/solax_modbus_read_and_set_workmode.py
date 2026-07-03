#!/usr/bin/env python3
"""SolaX Modbus Work Mode Control Script.

Script for reading and setting work mode on SolaX inverters via Modbus TCP.
Provides safe work mode control with strict validation and timing controls.

SAFETY FEATURES:
- Read operations: Safe system status checking
- Write operations: MASTER INVERTER ONLY with strict validation
- Mode validation: Only four predefined work modes allowed
- Timing compliance: 3-second delays between operations per SolaX requirements

Supported Work Modes:
- Self-Use: Normal operation mode (default)
- Charge: Force charge batteries from grid
- Discharge: Force discharge batteries to grid
- Hold: Stop battery charge/discharge (PV + grid only)

Usage:
    # Read current work mode (default behavior)
    python3 solax_modbus_read_and_set_workmode.py

    # Set work mode to self-use
    python3 solax_modbus_read_and_set_workmode.py --self-use

    # Set work mode to charge
    python3 solax_modbus_read_and_set_workmode.py --charge

    # Set work mode to discharge
    python3 solax_modbus_read_and_set_workmode.py --discharge
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytz


def find_project_root() -> Path:
    """Find the project root directory."""
    current_path = Path.cwd().resolve()

    for path in [current_path, *list(current_path.parents)]:
        tests_dir = path / "tests"
        src_dir = path / "src"

        if tests_dir.exists() and src_dir.exists():
            return path

    return current_path


# Add the src directory to Python path
project_root = find_project_root()
sys.path.append(str(project_root))

# Import modules (must be after sys.path setup)
try:
    from common_config_utils import find_config_file

    from src.api_clients.solax_modbus_client import (
        solax_modbus_set_work_mode,
        solax_modbus_work_mode,
    )
    from src.config_manager import load_static_config
    from src.core_logic.battery_simulation import BatteryMode, battery_mode_to_display_string
except ImportError as e:
    print("❌ FATAL ERROR: Cannot import required modules")
    print(f"   Error: {e}")
    print(f"   Project root: {project_root}")
    print("   Please ensure you are running from the project root directory.")
    sys.exit(1)

# Configure basic logging (will be updated based on command line args)
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)


def parse_arguments() -> argparse.Namespace:
    """Parse command line arguments with work mode options."""
    parser = argparse.ArgumentParser(
        description="SolaX Modbus Work Mode Control Script",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Work Mode Options (mutually exclusive):
  --self-use      Set inverter to Self-Use mode (normal operation)
  --charge        Set inverter to Force Charge mode (charge from grid)
  --discharge     Set inverter to Force Discharge mode (discharge to grid)
  --hold          Set inverter to Hold mode (stop charge/discharge, PV+grid only)

Examples:
  # Read current work mode (default behavior)
  python3 solax_modbus_read_and_set_workmode.py

  # Set to self-use mode with debug logging
  python3 solax_modbus_read_and_set_workmode.py --self-use --log-level DEBUG

  # Set to charge mode
  python3 solax_modbus_read_and_set_workmode.py --charge

  # Set to discharge mode
  python3 solax_modbus_read_and_set_workmode.py --discharge

  # Specify custom config file
  python3 solax_modbus_read_and_set_workmode.py --config /path/to/config.yaml --charge

SAFETY WARNING:
  Write operations modify inverter behavior and affect the MASTER INVERTER ONLY.
  Only use write operations when you understand the implications for your system.
        """,
    )

    # Configuration options
    parser.add_argument(
        "--config",
        "-c",
        type=str,
        help="Path to configuration file. If not specified, searches current directory, project root, and parent directories for config.yaml",
    )
    parser.add_argument(
        "--log-level",
        "-l",
        type=str,
        choices=["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"],
        default="INFO",
        help="Set logging level (default: INFO)",
    )

    # Work mode options (mutually exclusive)
    mode_group = parser.add_mutually_exclusive_group()
    mode_group.add_argument(
        "--self-use", action="store_true", help="Set inverter to Self-Use mode (normal operation)"
    )
    mode_group.add_argument(
        "--charge", action="store_true", help="Set inverter to Force Charge mode (charge from grid)"
    )
    mode_group.add_argument(
        "--discharge",
        action="store_true",
        help="Set inverter to Force Discharge mode (discharge to grid)",
    )
    mode_group.add_argument(
        "--hold",
        action="store_true",
        help="Set inverter to Hold mode (stop charge/discharge, PV+grid only)",
    )

    # Safety options
    parser.add_argument(
        "--force-mode-change",
        action="store_true",
        help="Bypass safety timing restrictions for mode changes (USE WITH CAUTION)",
    )

    return parser.parse_args()


def load_config(config_path: str | None = None) -> dict[str, Any] | None:
    """Load configuration with automatic config file finding."""
    try:
        # Use provided config path or find automatically
        if config_path:
            logger.info("Using specified config file: %s", config_path)
            if not Path(config_path).exists():
                logger.error("Specified config file not found: %s", config_path)
                return None
            final_config_path = config_path
        else:
            final_config_path = find_config_file(None, logger)

        return load_static_config(final_config_path)

    except (OSError, ValueError, KeyError, TypeError):
        logger.exception("Error loading configuration")
        return None


def read_current_work_mode(config: dict[str, Any]) -> BatteryMode | None:
    """Read and display current work mode from master inverter."""
    try:
        logger.info("Reading current work mode from master inverter...")

        current_mode = solax_modbus_work_mode(config)

        if current_mode:
            # Convert BatteryMode enum to display string for user output
            mode_display = battery_mode_to_display_string(current_mode)
            print("\n=== CURRENT WORK MODE ===")
            print(f"System Mode: {mode_display}")
            # Display UK local time with timezone indicator (BST/GMT)
            uk_tz = pytz.timezone("Europe/London")
            uk_time = datetime.now(tz=UTC).astimezone(uk_tz)
            tz_name = uk_time.strftime("%Z")  # BST or GMT
            print(f"Timestamp: {uk_time.strftime('%Y-%m-%d %H:%M:%S')} {tz_name}")
            print("=" * 25)
            return current_mode
        print("\n❌ Unable to read current work mode from inverter")
        print("💡 This could indicate:")
        print("   • Network connectivity issues with the inverter")
        print("   • Incorrect IP address configuration")
        print("   • Modbus TCP service not running on inverter")
        print("   • Temporary inverter communication failure")
        logger.error("Cannot read current inverter mode - communication failed")
        return None

    except ImportError:
        logger.exception("Error importing Modbus client")
        print("\n❌ Error: SolaX Modbus client not available")
        return None
    except (OSError, ValueError, RuntimeError) as e:
        logger.exception("Error reading work mode")
        print(f"\n❌ Error reading work mode: {e}")
        return None


def _display_safety_warning(mode_display: str, *, force_unsafe: bool) -> None:
    """Display safety warning before mode change operation."""
    print(f"\n⚠️  WARNING: Setting work mode to '{mode_display}' on MASTER INVERTER")
    print("This will modify inverter behavior and affect system operation.")

    if force_unsafe:
        print("🔥 SAFETY BYPASSED: --force-mode-change flag is active")
        print("⚠️  This bypasses all timing safety restrictions")

    print("Press Ctrl+C within 5 seconds to cancel...")


def _wait_for_user_confirmation() -> None:
    """Wait 5 seconds for user to cancel operation."""
    for i in range(5, 0, -1):
        print(f"Proceeding in {i} seconds...", end="\r")
        time.sleep(1)
    print("\nProceeding with work mode change...")


def _report_mode_change_error(result: dict[str, Any], *, force_unsafe: bool) -> None:
    """Report detailed error message based on mode change failure type."""
    error_type = result.get("error_type")
    error_message = result.get("error_message", "Unknown error")

    if error_type == "safety_interval":
        print(f"⚠️  Safety timing restriction: {result.get('safety_message', error_message)}")
        if not force_unsafe:
            print("🔧 Options:")
            print("   • Wait and try again")
            print("   • Use --force-mode-change to bypass (⚠️ Risk of hardware damage)")
    elif error_type == "mode_read_failed":
        print("❌ Cannot read current inverter mode (communication failure)")
        print("🔧 Check:")
        print("   • Network connectivity to inverter")
        print("   • Modbus TCP is enabled on inverter")
        print("   • IP addresses are correct in config")
    elif error_type == "hardware_failure":
        print("❌ Hardware write operation failed")
        print("🔧 Check:")
        print("   • Inverter is responding to Modbus commands")
        print("   • No hardware faults on inverter")
    elif error_type == "lock_timeout":
        print("❌ System busy - another process is changing modes")
        print("🔧 Try again in a few seconds")
    else:
        print(f"❌ Error: {result.get('message', error_message)}")
        if error_type:
            print(f"   Error type: {error_type}")


def set_work_mode(
    config: dict[str, Any], mode: BatteryMode, *, force_unsafe: bool = False
) -> bool | None:
    """Set work mode on master inverter with safety validation.

    Args:
        config: Configuration dictionary
        mode: BatteryMode enum value to set
        force_unsafe: If True, bypass safety timing checks

    Returns:
        bool: True if mode change succeeded, False otherwise

    """
    try:
        # Convert enum to display string for user-facing output
        mode_display = battery_mode_to_display_string(mode)

        # Display safety warning
        _display_safety_warning(mode_display, force_unsafe=force_unsafe)

        # Wait for user confirmation
        _wait_for_user_confirmation()

        logger.info(
            "Setting work mode to %r (BatteryMode: %s) on master inverter (force_unsafe=%s)",
            mode_display,
            mode.value,
            force_unsafe,
        )

        # Pass BatteryMode enum directly to API (following architecture)
        result = solax_modbus_set_work_mode(
            config, mode, changed_by="script", force_unsafe=force_unsafe
        )

        if result["success"]:
            from_mode = result.get("from_mode", "Unknown")
            to_mode = result.get("to_mode", mode_display)
            print(f"\n✅ Successfully changed work mode from {from_mode} to {to_mode}")
            logger.info("Work mode successfully changed from %s to %s", from_mode, to_mode)
            return True

        # Report error details
        print(f"\n❌ Failed to set work mode to '{mode_display}'")
        _report_mode_change_error(result, force_unsafe=force_unsafe)

        error_message = result.get("error_message", "Unknown error")
        logger.error("Failed to set work mode to '%s': %s", mode_display, error_message)
        return False

    except KeyboardInterrupt:
        print("\n\n⛔ Operation cancelled by user")
        logger.info("Work mode change cancelled by user")
        return False
    except ImportError:
        logger.exception("Error importing Modbus client")
        print("\n❌ Error: SolaX Modbus client not available")
        return False
    except (OSError, ValueError, RuntimeError) as e:
        logger.exception("Error setting work mode")
        print(f"\n❌ Error setting work mode: {e}")
        return False


def validate_modbus_configuration(config: dict[str, Any]) -> bool | None:
    """Validate that Modbus TCP is properly configured."""
    try:
        solax_config = config.get("solaX_cloud_api", {})

        if not solax_config.get("modbus_enabled", False):
            print("❌ Error: Modbus TCP is not enabled in configuration")
            print("Please set 'modbus_enabled: true' in config.yaml")
            return False

        master_ip = solax_config.get("master_ip")
        if not master_ip or master_ip == "YOUR_IP_HERE":
            print("❌ Error: Master inverter IP address not configured")
            print("Please set 'master_ip' in config.yaml")
            return False

        logger.info("Modbus TCP configuration validated successfully")
        return True

    except (OSError, ValueError, KeyError, TypeError) as e:
        logger.exception("Error validating configuration")
        print(f"❌ Error validating configuration: {e}")
        return False


def validate_arguments(args: argparse.Namespace) -> bool:
    """Validate command line arguments for work mode operations."""
    # Count how many mode flags were specified
    mode_count = sum([args.self_use, args.charge, args.discharge, args.hold])

    if mode_count > 1:
        print("❌ Error: Only one work mode can be specified at a time")
        print("Use --self-use, --charge, --discharge, or --hold (mutually exclusive)")
        return False

    # All validation passed
    return True


def _determine_write_mode(args: argparse.Namespace) -> BatteryMode | None:
    """Determine requested battery mode from command-line arguments."""
    if args.self_use:
        return BatteryMode.SELF_USE
    if args.charge:
        return BatteryMode.FORCE_CHARGE
    if args.discharge:
        return BatteryMode.FORCE_DISCHARGE
    if args.hold:
        return BatteryMode.MANUAL_STOP
    return None


def _verify_mode_change(
    current_mode: BatteryMode, new_mode: BatteryMode, write_mode: BatteryMode
) -> None:
    """Verify mode change was successful and report results."""
    if new_mode == write_mode:
        new_mode_display = battery_mode_to_display_string(new_mode)
        current_mode_display = battery_mode_to_display_string(current_mode)
        print(f"\n🎉 SUCCESS: Work mode successfully changed to '{new_mode_display}'")
        logger.info("Work mode change verified: %s → %s", current_mode_display, new_mode_display)
    else:
        expected_mode_display = battery_mode_to_display_string(write_mode)
        new_mode_display = battery_mode_to_display_string(new_mode)
        print(
            f"\n⚠️  WARNING: Expected mode '{expected_mode_display}', but got '{new_mode_display}'"
        )
        print("The change may need more time to take effect or may have failed")
        logger.warning(
            "Work mode verification mismatch: expected %r, got %r",
            expected_mode_display,
            new_mode_display,
        )


def main() -> int:  # pylint: disable=too-many-return-statements  # CLI validation with early returns for each error condition
    """Implement the work mode control sequence."""
    args = parse_arguments()

    # Configure logging level
    logging.getLogger().setLevel(getattr(logging, args.log_level))

    print("SolaX Modbus Work Mode Control Script")
    print("=" * 40)

    # Validate arguments
    if not validate_arguments(args):
        return 1

    # Load configuration
    config = load_config(args.config)
    if not config:
        print("❌ Failed to load configuration")
        return 1

    # Validate Modbus configuration
    if not validate_modbus_configuration(config):
        return 1

    # Determine requested operation (convert to BatteryMode enum immediately)
    write_mode = _determine_write_mode(args)

    # Sequence: Read → Display → [Wait 3s → Set mode → Wait 3s] → Read → Display

    # Step 1: Read current work mode
    print("\n📖 Step 1: Reading current work mode...")
    current_mode = read_current_work_mode(config)
    if current_mode is None:
        return 1

    # If no write mode specified, just show current mode and exit
    if write_mode is None:
        print("\n✅ Current work mode displayed successfully")
        print("Use --self-use, --charge, --discharge, or --hold to change work mode")
        return 0

    # Step 2: Wait 3 seconds before write operation
    mode_display = battery_mode_to_display_string(write_mode)
    print(f"\n⏱️  Step 2: Waiting 3 seconds before setting mode to '{mode_display}'...")
    time.sleep(3)

    # Step 3: Set new work mode
    print(f"\n✍️  Step 3: Setting work mode to '{mode_display}'...")
    set_result = set_work_mode(config, write_mode, force_unsafe=args.force_mode_change)
    if not set_result:
        return 1

    # Step 4: Wait 3 seconds after write operation
    print("\n⏱️  Step 4: Waiting 3 seconds after work mode change...")
    time.sleep(3)

    # Step 5: Read work mode again to confirm change
    print("\n📖 Step 5: Reading work mode to confirm change...")
    new_mode = read_current_work_mode(config)
    if new_mode is None:
        print("⚠️  Warning: Could not verify work mode change")
        return 1

    # Verify the change was successful
    _verify_mode_change(current_mode, new_mode, write_mode)

    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\n\n⛔ Script interrupted by user")
        sys.exit(1)
    except (OSError, ValueError, RuntimeError) as e:
        logger.exception("Unexpected error")
        print(f"\n❌ Unexpected error: {e}")
        sys.exit(1)
