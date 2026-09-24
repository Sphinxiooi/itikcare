from django.urls import path

from . import views

urlpatterns = [
    path("notifications/read-all/", views.mark_all_read, name="notifications_mark_all_read"),
    path("notifications/dismiss/", views.dismiss, name="notifications_dismiss"),
]
