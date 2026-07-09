from django.contrib import admin

from .models import DailyLog, DailyLogEdit, Flock


@admin.register(Flock)
class FlockAdmin(admin.ModelAdmin):
    list_display = ("generation_number", "started_on", "is_active")


@admin.register(DailyLog)
class DailyLogAdmin(admin.ModelAdmin):
    list_display = ("date", "flock", "egg_count", "flock_size", "flock_age_weeks", "recorded_by")
    list_filter = ("flock",)
    date_hierarchy = "date"


@admin.register(DailyLogEdit)
class DailyLogEditAdmin(admin.ModelAdmin):
    """Read-only in the admin: this is an audit trail, not user-editable data."""

    list_display = ("daily_log", "field_name", "old_value", "new_value", "changed_by", "changed_at")

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False
