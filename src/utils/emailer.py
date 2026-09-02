"""Minimal SMTP email sending, for scripts/weekly_health_check.py's alerts.

Gmail SMTP + App Password only (see config.yaml's email: comments) - no
OAuth2, no HTML, no attachments. Deliberately small: this project's only
current use case is a plain-text alert sent at most weekly.
"""

from __future__ import annotations

import logging
import smtplib
from email.message import EmailMessage
from typing import Any

logger = logging.getLogger(__name__)

DEFAULT_SMTP_HOST = "smtp.gmail.com"
DEFAULT_SMTP_PORT = 587
DEFAULT_TIMEOUT_SECONDS = 30.0


def send_email(config: dict[str, Any], subject: str, body: str) -> bool:
    """Send a plain-text email via the email: section of config.yaml.

    Args:
        config: Full static config - reads its "email" section (username,
            password, to_address expected, password merged in from
            secrets.yaml).
        subject: Email subject line.
        body: Plain-text email body.

    Returns:
        True if the email was sent. False if email is disabled/misconfigured,
        or sending failed for any reason (fail-fast, matches this codebase's
        other integrations - a failed alert must not crash the caller).

    """
    try:
        return _send_email_unsafe(config, subject, body)
    except Exception:
        # Circuit Breaker: sending the alert must never crash the health
        # check that's trying to report a problem.
        logger.exception("Unexpected error sending email")
        return False


def _send_email_unsafe(config: dict[str, Any], subject: str, body: str) -> bool:
    email_config = config.get("email", {})
    if not email_config.get("enabled", False):
        logger.info("Email is disabled (email.enabled: false), not sending")
        return False

    username = email_config.get("username")
    password = email_config.get("password")
    to_address = email_config.get("to_address")
    if not username or not password or not to_address:
        logger.error(
            "email.username/password/to_address are not all set - see config.yaml's "
            "email comments"
        )
        return False

    smtp_host = email_config.get("smtp_host", DEFAULT_SMTP_HOST)
    smtp_port = email_config.get("smtp_port", DEFAULT_SMTP_PORT)
    timeout_seconds = email_config.get("timeout_seconds", DEFAULT_TIMEOUT_SECONDS)

    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = username
    message["To"] = to_address
    message.set_content(body)

    with smtplib.SMTP(smtp_host, smtp_port, timeout=timeout_seconds) as smtp:
        smtp.starttls()
        smtp.login(username, password)
        smtp.send_message(message)

    logger.info("Email sent to %s: %s", to_address, subject)
    return True
