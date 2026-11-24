"""URL configuration (login, signup and password reset are wired explicitly to use the project's own forms and rate limiting)."""
from django.conf import settings
from django.contrib import admin
from django.contrib.auth import views as auth_views
from django.urls import include, path, re_path
from django.views.static import serve

from accounts import session_views, views as accounts_views
from accounts.forms import StyledAuthenticationForm

urlpatterns = [
    path('admin/', admin.site.urls),
    # Language switch (POST only)
    path('i18n/', include('django.conf.urls.i18n')),
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
    path('account/session/status/', session_views.session_status, name='session_status'),
    path('account/session/extend/', session_views.session_extend, name='session_extend'),
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
    path('', include('notifications.urls')),
]

# Serve uploaded avatars (WhiteNoise doesn't serve MEDIA_ROOT)
urlpatterns += [
    re_path(
        r'^%s(?P<path>.*)$' % settings.MEDIA_URL.lstrip('/'),
        serve,
        {'document_root': settings.MEDIA_ROOT},
    ),
]
