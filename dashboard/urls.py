from django.urls import path

from . import views

urlpatterns = [
    path("", views.index, name="dashboard"),
    path("about/researchers/", views.researchers, name="researchers"),
    path("robots.txt", views.robots_txt, name="robots_txt"),
    path("google6c0aee7b83489d73.html", views.google_site_verification, name="google_site_verification"),
]
