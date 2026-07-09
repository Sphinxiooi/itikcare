from django.contrib import admin

from .models import Recommendation


@admin.register(Recommendation)
class RecommendationAdmin(admin.ModelAdmin):
    list_display = ("forecast", "triggered_by", "priority", "created_at")
    list_filter = ("priority", "triggered_by")
