from django.conf import settings
from django.db import models


class NotificationReceipt(models.Model):
    """Stores what a farmer did with a bell notification (read_at, dismissed_at), keyed by the notification key.

    Notifications themselves are computed live (notifications/services.py).
    """

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="notification_receipts"
    )
    key = models.CharField(max_length=120)
    read_at = models.DateTimeField(null=True, blank=True)
    dismissed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["user", "key"], name="unique_notification_receipt_per_user_key")
        ]

    def __str__(self):
        return f"{self.user} — {self.key}"


class PushSubscription(models.Model):
    """One phone (browser) that turned on phone notifications for a farmer.

    The endpoint and two keys come from the browser's Push API and are all that is needed
    to send it an encrypted message (notifications/push.py).
    """

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="push_subscriptions"
    )
    # Unique per browser: if another farmer turns it on, the row moves to them
    endpoint = models.URLField(max_length=500, unique=True)
    p256dh = models.CharField(max_length=120)
    auth = models.CharField(max_length=40)
    # The scheduled job has no request, so the farmer's language is saved here
    language = models.CharField(max_length=10, default="en")
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"{self.user} — push subscription {self.pk}"
