"""Minimal email sending via msmtp, for scripts/weekly_health_check.py's alerts.

Shells out to the system's `msmtp` binary rather than talking SMTP directly -
the Pi this project deploys to already has msmtp configured and working for
the target Gmail account (~/.msmtprc, already used by its existing
home_backup/backup_PI scripts), so this reuses that proven setup instead of
asking for a second, separate credential (an SMTP password) to be generated
and stored in secrets.yaml. msmtp itself decides which account/credentials to
use (its default account, unless email.msmtp_account picks a named one) -
this module never touches a password.
"""

from __future__ import annotations

import logging
import subprocess
from typing import Any

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT_SECONDS = 30.0
DEFAULT_MSMTP_BINARY = "msmtp"


def send_email(config: dict[str, Any], subject: str, body: str) -> bool:
    """Send a plain-text email via msmtp, using the email: section of config.yaml.

    Args:
        config: Full static config - reads its "email" section (to_address
            required; from_address, msmtp_binary, msmtp_account, timeout_seconds
            all optional).
        subject: Email subject line.
        body: Plain-text email body.

    Returns:
        True if msmtp accepted the message (exit code 0). False if email is
        disabled/misconfigured, the msmtp binary isn't found, or sending
        failed for any reason (fail-fast, matches this codebase's other
        integrations - a failed alert must not crash the caller).

    """
    try:
        return _send_email_unsafe(config, subject, body)
    except Exception:
        # Circuit Breaker: sending the alert must never crash the health
        # check that's trying to report a problem.
        logger.exception("Unexpected error sending email via msmtp")
        return False


def _send_email_unsafe(config: dict[str, Any], subject: str, body: str) -> bool:
    email_config = config.get("email", {})
    if not email_config.get("enabled", False):
        logger.info("Email is disabled (email.enabled: false), not sending")
        return False

    to_address = email_config.get("to_address")
    if not to_address:
        logger.error("email.to_address is not set - see config.yaml's email comments")
        return False

    from_address = email_config.get("from_address")
    msmtp_account = email_config.get("msmtp_account")
    msmtp_binary = email_config.get("msmtp_binary", DEFAULT_MSMTP_BINARY)
    timeout_seconds = email_config.get("timeout_seconds", DEFAULT_TIMEOUT_SECONDS)

    header_lines = [f"To: {to_address}", f"Subject: {subject}"]
    if from_address:
        header_lines.append(f"From: {from_address}")
    message = "\n".join([*header_lines, "", body])

    command = [msmtp_binary]
    if msmtp_account:
        command.extend(["-a", msmtp_account])
    command.extend(["--", to_address])

    try:
        result = subprocess.run(
            command,
            input=message,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            check=False,
        )
    except FileNotFoundError:
        logger.error(
            "msmtp binary (%r) not found - install/configure msmtp, or set "
            "email.msmtp_binary to its path",
            msmtp_binary,
        )
        return False

    if result.returncode != 0:
        logger.error("msmtp failed (exit %s): %s", result.returncode, result.stderr.strip())
        return False

    logger.info("Email sent to %s via msmtp: %s", to_address, subject)
    return True
