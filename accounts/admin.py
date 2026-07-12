from django.contrib import admin
from django.contrib.auth.admin import UserAdmin

from .models import User


@admin.register(User)
class ItikCareUserAdmin(UserAdmin):
    fieldsets = UserAdmin.fieldsets + (
        ("Farm role", {"fields": ("role", "is_foundation_farmer")}),
    )
    list_display = UserAdmin.list_display + ("role", "is_foundation_farmer")
    list_filter = UserAdmin.list_filter + ("role", "is_foundation_farmer")
