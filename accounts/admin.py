from django.contrib import admin
from django.contrib.auth.admin import UserAdmin

from .models import User


@admin.register(User)
class ItikCareUserAdmin(UserAdmin):
    fieldsets = UserAdmin.fieldsets + (
        ("Farm role", {"fields": ("role", "is_foundation_farmer")}),
        ("Contact & consent", {"fields": ("phone_number", "privacy_consented_at")}),
    )
    list_display = UserAdmin.list_display + ("phone_number", "role", "is_foundation_farmer")
    # Farmers sign up with a name + email-or-phone (no username they'd remember), so an
    # admin doing a manual password reset needs to find them by those instead.
    search_fields = UserAdmin.search_fields + ("phone_number",)
    list_filter = UserAdmin.list_filter + ("role", "is_foundation_farmer")
