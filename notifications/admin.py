from django.contrib import admin

from .models import NotificationReceipt


@admin.register(NotificationReceipt)
class NotificationReceiptAdmin(admin.ModelAdmin):
    """Read-only: receipts are written only by the bell's own read/dismiss views."""

    list_display = ("user", "key", "read_at", "dismissed_at")
    list_filter = ("user",)
    readonly_fields = ("user", "key", "read_at", "dismissed_at")

    def has_add_permission(self, request):
        return False
