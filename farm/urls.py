from django.urls import path

from . import views

urlpatterns = [
    path("log-daily-data/", views.log_daily_data, name="log_daily_data"),
    path("farm-records/", views.farm_records, name="farm_records"),
    path("farm-records/<int:pk>/edit/", views.farm_record_edit, name="farm_record_edit"),
    path("farm-records/<int:pk>/delete/", views.farm_record_delete, name="farm_record_delete"),
    path("farm-records/import/", views.import_csv, name="import_csv"),
    path("farm-records/import/template/", views.download_import_csv_template, name="download_import_csv_template"),
    path("flock/", views.flock_profile, name="flock_profile"),
    path("flock/retire/", views.flock_retire, name="flock_retire"),
    path("flock/toggle-caging/", views.toggle_caging_status, name="toggle_caging_status"),
    path("flock/resume-caging/", views.resume_caging, name="resume_caging"),
]
