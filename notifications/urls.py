from django.urls import path

from . import views

urlpatterns = [
    path("notifications/read-all/", views.mark_all_read, name="notifications_mark_all_read"),
    path("notifications/dismiss/", views.dismiss, name="notifications_dismiss"),
    path("notifications/push/status/", views.push_status, name="push_status"),
    path("notifications/push/subscribe/", views.push_subscribe, name="push_subscribe"),
    path("notifications/push/unsubscribe/", views.push_unsubscribe, name="push_unsubscribe"),
    path("notifications/push/test/", views.push_test, name="push_test"),
    # Both must sit at the site root
    path("sw.js", views.service_worker, name="service_worker"),
    path("manifest.webmanifest", views.web_manifest, name="web_manifest"),
]
