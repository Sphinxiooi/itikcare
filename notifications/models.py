from django.conf import settings
from django.db import models


class NotificationReceipt(models.Model):
    """Remembers what a farmer has done with one header-bell notification.

    Notifications themselves are never stored -- notifications/services.py computes
    them live from DailyLog/Flock/Forecast/User state on every page load, so one
    disappears on its own as soon as the underlying issue is fixed (e.g. today's log
    gets entered) and can never go stale. This table only records the farmer's
    *reaction* to a notification, keyed by that notification's `key`:

    * read_at -- set when the bell's dropdown is opened; clears the unread badge.
    * dismissed_at -- set by the item's dismiss button; hides it from the list.

    Keys embed the date/object they're about (e.g. "log_today_missing:2026-09-24"), so
    reading or dismissing today's reminder never silences tomorrow's.
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
