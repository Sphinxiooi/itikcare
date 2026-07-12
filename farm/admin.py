from django.contrib import admin

from .models import DailyLog, DailyLogEdit, Flock

# No per-tenant filtering is needed here: only is_staff accounts can reach /admin/ at
# all (Django's own AdminSite.has_permission), and self-registered farmers never get
# is_staff (accounts.User.save() only sets is_superuser->role=admin, never is_staff;
# only createsuperuser grants it). So every farm's Flock/DailyLog being visible here to
# a staff user is deliberate cross-farm oversight, not a tenant-isolation leak.


@admin.register(Flock)
class FlockAdmin(admin.ModelAdmin):
    list_display = ("generation_number", "owner", "started_on", "is_active")
    list_filter = ("owner",)


@admin.register(DailyLog)
class DailyLogAdmin(admin.ModelAdmin):
    list_display = ("date", "flock", "egg_count", "flock_size", "flock_age_weeks", "recorded_by")
    list_filter = ("flock__owner", "flock")
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
