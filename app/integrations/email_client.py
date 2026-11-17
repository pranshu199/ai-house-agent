"""SMTP email delivery for realtor notifications."""

import os
import re
import smtplib
from email.message import EmailMessage
from email.utils import make_msgid


_EMAIL_PATTERN = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_SMTP_TIMEOUT_SECONDS = 10


class EmailConfigurationError(RuntimeError):
    """Raised when SMTP notification settings are incomplete or invalid."""


def send_notification_email(recipient: str, subject: str, body: str) -> str:
    """Send a plain-text notification email and return its message ID."""

    host = os.getenv("SMTP_HOST", "").strip()
    sender = os.getenv("SMTP_FROM_EMAIL", "").strip()
    username = os.getenv("SMTP_USERNAME", "").strip()
    password = os.getenv("SMTP_PASSWORD", "")
    try:
        port = int(os.getenv("SMTP_PORT", "587"))
    except ValueError as exc:
        raise EmailConfigurationError("SMTP_PORT must be an integer") from exc

    if not host or not _EMAIL_PATTERN.fullmatch(sender):
        raise EmailConfigurationError("SMTP host and sender email are required")
    if not 1 <= port <= 65535 or bool(username) != bool(password):
        raise EmailConfigurationError("SMTP settings are invalid")
    if not _EMAIL_PATTERN.fullmatch(recipient):
        raise EmailConfigurationError("Notification recipient email is invalid")

    message = EmailMessage()
    message["From"] = sender
    message["To"] = recipient
    message["Subject"] = subject
    message["Message-ID"] = make_msgid()
    message.set_content(body)

    use_tls = os.getenv("SMTP_USE_TLS", "true").strip().casefold() not in {
        "0",
        "false",
        "no",
    }
    if port == 465:
        with smtplib.SMTP_SSL(
            host,
            port,
            timeout=_SMTP_TIMEOUT_SECONDS,
        ) as smtp:
            if username:
                smtp.login(username, password)
            smtp.send_message(message)
    else:
        with smtplib.SMTP(
            host,
            port,
            timeout=_SMTP_TIMEOUT_SECONDS,
        ) as smtp:
            if use_tls:
                smtp.starttls()
            if username:
                smtp.login(username, password)
            smtp.send_message(message)
    return message["Message-ID"]
