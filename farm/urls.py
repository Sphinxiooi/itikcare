from django.urls import path

from . import views

urlpatterns = [
    path("log-daily-data/", views.log_daily_data, name="log_daily_data"),
    path("farm-records/", views.farm_records, name="farm_records"),
    path("farm-records/<int:pk>/edit/", views.farm_record_edit, name="farm_record_edit"),
]
