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
