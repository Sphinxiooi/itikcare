"""URL configuration for the itikcare project.

Login/logout/signup/password-reset are wired explicitly (rather than including the
full django.contrib.auth.urls) to keep the URL surface easy to reason about and to
attach this project's own styled forms/templates/rate limiting to each view.
"""
from django.conf import settings
from django.contrib import admin
from django.contrib.auth import views as auth_views
from django.urls import include, path, re_path
from django.views.static import serve

from accounts import views as accounts_views
from accounts.forms import StyledAuthenticationForm

urlpatterns = [
    path('admin/', admin.site.urls),
    path(
        'accounts/login/',
        accounts_views.RateLimitedLoginView.as_view(
            authentication_form=StyledAuthenticationForm,
            template_name='registration/login.html',
        ),
        name='login',
    ),
    path('accounts/logout/', auth_views.LogoutView.as_view(), name='logout'),
    path('accounts/signup/', accounts_views.signup, name='signup'),
    path('accounts/google/login/', accounts_views.google_login, name='google_login'),
    path('accounts/google/callback/', accounts_views.google_callback, name='google_callback'),
    path('account/settings/', accounts_views.account_settings, name='account_settings'),
    path('account/delete/', accounts_views.delete_account, name='delete_account'),
    path(
        'accounts/password-reset/',
        accounts_views.request_reset_code,
        name='password_reset',
    ),
    path(
        'accounts/password-reset/verify/',
        accounts_views.verify_reset_code,
        name='password_reset_verify',
    ),
    path(
        'accounts/reset/done/',
        auth_views.PasswordResetCompleteView.as_view(template_name='registration/password_reset_complete.html'),
        name='password_reset_complete',
    ),
    path('', include('dashboard.urls')),
    path('', include('farm.urls')),
    path('', include('forecasting.urls')),
]

# Farmer-uploaded avatars. whitenoise serves STATIC_ROOT but never MEDIA_ROOT, and
# django.conf.urls.static.static() is a no-op when DEBUG=False, so route MEDIA_URL through
# Django's own static-file view explicitly -- fine at single-farm scale. (On a VM, an nginx
# location block over MEDIA_ROOT can take over this job; this route is then simply unused.)
urlpatterns += [
    re_path(
        r'^%s(?P<path>.*)$' % settings.MEDIA_URL.lstrip('/'),
        serve,
        {'document_root': settings.MEDIA_ROOT},
    ),
]
