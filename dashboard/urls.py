from django.urls import path

from . import views

urlpatterns = [
    path("", views.index, name="dashboard"),
    path("about/researchers/", views.researchers, name="researchers"),
]
