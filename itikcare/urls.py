"""URL configuration for the itikcare project.

Only login/logout are wired explicitly (rather than including the full
django.contrib.auth.urls, which also adds password-reset/change flows not needed
for this session's scope) to keep the URL surface easy to reason about.
"""
from django.contrib import admin
from django.contrib.auth import views as auth_views
from django.urls import include, path

from accounts import views as accounts_views
from accounts.forms import StyledAuthenticationForm

urlpatterns = [
    path('admin/', admin.site.urls),
    path(
        'accounts/login/',
        auth_views.LoginView.as_view(
            authentication_form=StyledAuthenticationForm,
            template_name='registration/login.html',
        ),
        name='login',
    ),
    path('accounts/logout/', auth_views.LogoutView.as_view(), name='logout'),
    path('accounts/signup/', accounts_views.signup, name='signup'),
    path('', include('dashboard.urls')),
    path('', include('farm.urls')),
    path('', include('forecasting.urls')),
]
