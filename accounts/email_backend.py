"""Email backend that sends through Brevo's HTTPS API (for hosts that block outbound SMTP).

Selected when BREVO_API_KEY is set; DEFAULT_FROM_EMAIL must be a sender verified in Brevo.
"""

import base64
import logging
from email.utils import getaddresses, parseaddr

import requests
from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from django.core.mail.backends.base import BaseEmailBackend

logger = logging.getLogger(__name__)

BREVO_SEND_URL = "https://api.brevo.com/v3/smtp/email"
REQUEST_TIMEOUT_SECONDS = 15


class BrevoAPIError(Exception):
    """Brevo returned a non-2xx status."""


def _address(raw):
    """'Name <a@b.com>' or 'a@b.com' -> Brevo's {"email", "name"} dict."""
    name, email = parseaddr(raw)
    entry = {"email": email}
    if name:
        entry["name"] = name
    return entry


def _addresses(raw_list):
    return [{"email": email, **({"name": name} if name else {})}
            for name, email in getaddresses(raw_list) if email]


class BrevoEmailBackend(BaseEmailBackend):
    """Sends each EmailMessage as one POST to Brevo."""

    def __init__(self, api_key=None, fail_silently=False, **kwargs):
        super().__init__(fail_silently=fail_silently, **kwargs)
        self.api_key = api_key or getattr(settings, "BREVO_API_KEY", None)
        if not self.api_key:
            raise ImproperlyConfigured("BrevoEmailBackend needs BREVO_API_KEY to be set.")

    def send_messages(self, email_messages):
        """Return the number sent; errors propagate unless fail_silently=True."""
        sent = 0
        for message in email_messages:
            try:
                self._send(message)
            except Exception:
                if not self.fail_silently:
                    raise
                logger.exception("Brevo send failed (fail_silently=True)")
                continue
            sent += 1
        return sent

    def _build_payload(self, message):
        recipients = _addresses(message.to)
        if not recipients:
            raise ValueError("EmailMessage has no recipients.")

        payload = {
            "sender": _address(message.from_email),
            "to": recipients,
            "subject": message.subject,
        }
        if message.cc:
            payload["cc"] = _addresses(message.cc)
        if message.bcc:
            payload["bcc"] = _addresses(message.bcc)
        if message.reply_to:
            # Brevo accepts a single reply-to address
            payload["replyTo"] = _address(message.reply_to[0])

        # Body: plain text, with an HTML alternative or HTML-only message
        if message.content_subtype == "html":
            payload["htmlContent"] = message.body
        else:
            payload["textContent"] = message.body
        for content, mimetype in getattr(message, "alternatives", []):
            if mimetype == "text/html":
                payload["htmlContent"] = content

        attachments = self._build_attachments(message)
        if attachments:
            payload["attachment"] = attachments
        return payload

    @staticmethod
    def _build_attachments(message):
        attachments = []
        for attachment in message.attachments:
            if isinstance(attachment, tuple):
                filename, content, _mimetype = attachment
                if isinstance(content, str):
                    content = content.encode("utf-8")
            else:  # a MIMEBase part
                filename = attachment.get_filename()
                content = attachment.get_payload(decode=True)
            attachments.append({"name": filename, "content": base64.b64encode(content).decode("ascii")})
        return attachments

    def _send(self, message):
        response = requests.post(
            BREVO_SEND_URL,
            json=self._build_payload(message),
            headers={"api-key": self.api_key, "accept": "application/json"},
            timeout=REQUEST_TIMEOUT_SECONDS,
        )
        if not 200 <= response.status_code < 300:
            # Include Brevo's error text (never the api-key)
            raise BrevoAPIError(f"Brevo API returned {response.status_code}: {response.text[:300]}")
