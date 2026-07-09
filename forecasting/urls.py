from django.urls import path

from . import views

urlpatterns = [
    path("forecast-recommendations/", views.forecast_recommendations, name="forecast_recommendations"),
]
