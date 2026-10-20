"""Verify and reset mail. Tokens stay out of HTTP responses and out of logs."""

from __future__ import annotations

import json
import logging
import os
import smtplib
import urllib.error
import urllib.request
from email.message import EmailMessage

log = logging.getLogger("casesifter.mail")


def _sender() -> str:
    return (os.environ.get("SMTP_FROM") or os.environ.get("EMAIL_FROM") or "").strip()


def _resend_key() -> str:
    return (os.environ.get("RESEND_API_KEY") or "").strip()


def mailer_configured() -> bool:
    sender = _sender()
    if not sender:
        return False
    if _resend_key():
        return True
    return bool((os.environ.get("SMTP_HOST") or "").strip())


def send_auth_email(to_addr: str, subject: str, text: str) -> bool:
    """Send one plain-text message. Returns False when delivery is not configured or fails."""
    if not mailer_configured():
        return False
    sender = _sender()
    try:
        if _resend_key():
            return _send_resend(to_addr, subject, text, sender)
        return _send_smtp(to_addr, subject, text, sender)
    except Exception:
        log.warning("auth email send failed")
        return False


def _send_resend(to_addr: str, subject: str, text: str, sender: str) -> bool:
    body = json.dumps(
        {"from": sender, "to": [to_addr], "subject": subject, "text": text}
    ).encode("utf-8")
    request = urllib.request.Request(
        "https://api.resend.com/emails",
        data=body,
        method="POST",
        headers={
            "Authorization": f"Bearer {_resend_key()}",
            "Content-Type": "application/json",
            "User-Agent": "casesifter-control",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            return 200 <= int(response.status) < 300
    except urllib.error.HTTPError:
        log.warning("auth email provider rejected the message")
        return False


def _send_smtp(to_addr: str, subject: str, text: str, sender: str) -> bool:
    host = os.environ["SMTP_HOST"].strip()
    port = int(os.environ.get("SMTP_PORT") or "587")
    username = (os.environ.get("SMTP_USERNAME") or "").strip()
    password = os.environ.get("SMTP_PASSWORD") or ""
    use_tls = (os.environ.get("SMTP_USE_TLS") or "true").strip().lower() not in {
        "0",
        "false",
        "no",
    }
    message = EmailMessage()
    message["From"] = sender
    message["To"] = to_addr
    message["Subject"] = subject
    message.set_content(text)
    import ssl

    with smtplib.SMTP(host, port, timeout=20) as smtp:
        if use_tls:
            # TLS 1.2 minimum (prefer 1.3 when the server offers it).
            ctx = ssl.create_default_context()
            if hasattr(ssl, "TLSVersion"):
                ctx.minimum_version = ssl.TLSVersion.TLSv1_2
            smtp.starttls(context=ctx)
        if username:
            smtp.login(username, password)
        smtp.send_message(message)
    return True
