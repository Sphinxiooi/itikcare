"""Phone notifications through the browser's Web Push service.

The server encrypts a small message for each saved PushSubscription and posts it to the
browser vendor's push service, which delivers it to the phone even when ItikCare is closed.
The service worker (notifications/templates/notifications/sw.js) then displays it.
"""

import json
import logging
from datetime import timedelta
from urllib.parse import urlsplit

from django.conf import settings
from django.urls import reverse
from django.utils import timezone, translation
from django.utils.translation import gettext as _
from pywebpush import WebPushException, webpush

logger = logging.getLogger(__name__)

# Push services used by Android browsers (Chrome/Samsung/Edge use FCM; Firefox uses Mozilla).
# The server posts to the saved address, so anything else is refused.
ALLOWED_PUSH_HOSTS = ("fcm.googleapis.com", "updates.push.services.mozilla.com")

SEND_TIMEOUT_SECONDS = 10


def push_enabled():
    """True when the server has its VAPID key pair."""
    return bool(settings.VAPID_PUBLIC_KEY and settings.VAPID_PRIVATE_KEY)


def is_allowed_endpoint(endpoint):
    parts = urlsplit(endpoint)
    return parts.scheme == "https" and parts.netloc in ALLOWED_PUSH_HOSTS


def _seconds_left_today():
    """How long an undelivered message stays useful: until local midnight."""
    now = timezone.localtime()
    midnight = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    return max(int((midnight - now).total_seconds()), 60)


def send_push(subscription, payload):
    """Send one message to one phone. Returns True if the push service accepted it.

    A subscription the push service reports as gone (404/410) is deleted.
    """
    try:
        webpush(
            subscription_info={
                "endpoint": subscription.endpoint,
                "keys": {"p256dh": subscription.p256dh, "auth": subscription.auth},
            },
            data=json.dumps(payload),
            vapid_private_key=settings.VAPID_PRIVATE_KEY,
            # A new dict every call: pywebpush adds per-service claims to it
            vapid_claims={"sub": settings.VAPID_SUBJECT},
            ttl=_seconds_left_today(),
            # Ask Android to deliver now instead of waiting out battery saving
            headers={"Urgency": "high"},
            timeout=SEND_TIMEOUT_SECONDS,
        )
    except WebPushException as error:
        # `is not None`: a requests Response with a 4xx status is falsy
        status = error.response.status_code if error.response is not None else None
        if status in (404, 410):
            subscription.delete()
        else:
            logger.warning("Push failed for user_id=%s (status %s): %s", subscription.user_id, status, error)
        return False
    except Exception:
        logger.exception("Push failed for user_id=%s", subscription.user_id)
        return False
    return True


def missing_log_payload(language, today):
    """The "today's log is missing" message, in the language saved with the subscription."""
    with translation.override(language):
        return {
            # Same wording as the bell notification (notifications/services.py)
            "title": _("Today's log is missing"),
            "body": _("Record today's egg count, feed, and weather so your forecast stays up to date."),
            "url": reverse("log_daily_data"),
            # A repeat for the same day replaces the earlier one on the phone
            "tag": f"log-today-{today.isoformat()}",
        }


def send_missing_log_push(user, today):
    """Remind every phone the farmer turned on. Returns how many accepted the message."""
    if not push_enabled():
        return 0
    return sum(
        send_push(subscription, missing_log_payload(subscription.language, today))
        for subscription in user.push_subscriptions.all()
    )
