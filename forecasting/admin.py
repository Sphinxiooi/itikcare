from django.contrib import admin

from .models import Forecast


@admin.register(Forecast)
class ForecastAdmin(admin.ModelAdmin):
    list_display = ("forecast_date", "flock", "predicted_daily_yield", "predicted_tri_day_yield", "model_version")
    list_filter = ("flock", "model_version")
    date_hierarchy = "forecast_date"
