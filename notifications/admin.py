from django.contrib import admin

from .models import NotificationReceipt, PushSubscription


@admin.register(PushSubscription)
class PushSubscriptionAdmin(admin.ModelAdmin):
    """Which farmers have phone notifications on; the browser's keys are not shown."""

    list_display = ("user", "language", "created_at")
    list_filter = ("user",)
    fields = readonly_fields = ("user", "language", "created_at")

    def has_add_permission(self, request):
        return False


@admin.register(NotificationReceipt)
class NotificationReceiptAdmin(admin.ModelAdmin):
    """Read-only: receipts are written only by the bell's own read/dismiss views."""

    list_display = ("user", "key", "read_at", "dismissed_at")
    list_filter = ("user",)
    readonly_fields = ("user", "key", "read_at", "dismissed_at")

    def has_add_permission(self, request):
        return False
