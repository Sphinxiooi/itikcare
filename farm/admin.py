from django.contrib import admin

from .models import AUDITED_FIELDS, DailyLog, DailyLogEdit, Flock

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
    """Mirrors the lock and audit-trail rules farm.views.farm_record_edit/
    farm_record_delete enforce for farmer-facing edits, so a staff user going
    through /admin/ instead can't silently bypass either one (see CLAUDE.md:
    DailyLog edits must always be tracked, and rows already used to train a model
    are immutable)."""

    list_display = ("date", "flock", "egg_count", "flock_size", "flock_age_weeks", "recorded_by")
    list_filter = ("flock__owner", "flock")
    date_hierarchy = "date"

    def get_readonly_fields(self, request, obj=None):
        if obj is not None and obj.is_locked:
            return [f.name for f in self.model._meta.fields]
        return self.readonly_fields

    def has_delete_permission(self, request, obj=None):
        if obj is not None and obj.is_locked:
            return False
        return super().has_delete_permission(request, obj)

    def save_model(self, request, obj, form, change):
        old_values = None
        if change:
            old_values = {
                field_name: getattr(DailyLog.objects.get(pk=obj.pk), field_name)
                for field_name in AUDITED_FIELDS
            }
        super().save_model(request, obj, form, change)
        if old_values is not None:
            for field_name in AUDITED_FIELDS:
                new_value = getattr(obj, field_name)
                old_value = old_values[field_name]
                if old_value != new_value:
                    DailyLogEdit.objects.create(
                        daily_log=obj,
                        field_name=field_name,
                        old_value=str(old_value),
                        new_value=str(new_value),
                        changed_by=request.user,
                    )


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
