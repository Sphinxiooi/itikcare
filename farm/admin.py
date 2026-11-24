from django.contrib import admin

from .models import AUDITED_FIELDS, DailyLog, DailyLogEdit, DailyLogReminder, Flock

# Only staff can open /admin/, so seeing every farm's data here is deliberate oversight


@admin.register(Flock)
class FlockAdmin(admin.ModelAdmin):
    list_display = ("generation_number", "owner", "started_on", "is_active")
    list_filter = ("owner",)


@admin.register(DailyLog)
class DailyLogAdmin(admin.ModelAdmin):
    """Same lock and audit-trail rules as the farmer edit view (locked rows are read-only; edits write DailyLogEdit rows)."""

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
    """Read-only audit trail."""

    list_display = ("daily_log", "field_name", "old_value", "new_value", "changed_by", "changed_at")

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(DailyLogReminder)
class DailyLogReminderAdmin(admin.ModelAdmin):
    """Read-only log of sent reminder emails."""

    list_display = ("owner", "flock", "reminder_date", "sent_at")
    list_filter = ("owner",)
    date_hierarchy = "reminder_date"

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False
