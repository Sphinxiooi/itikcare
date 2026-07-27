"""URL configuration for the itikcare project.

Login/logout/signup/password-reset are wired explicitly (rather than including the
full django.contrib.auth.urls) to keep the URL surface easy to reason about and to
attach this project's own styled forms/templates/rate limiting to each view.
"""
from django.conf import settings
from django.conf.urls.static import static
from django.contrib import admin
from django.contrib.auth import views as auth_views
from django.urls import include, path

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

if settings.DEBUG:
    # Dev-only: whitenoise (MIDDLEWARE) serves STATIC_ROOT but not MEDIA_ROOT -- see
    # MEDIA_URL's comment in settings.py for the production media-serving caveat.
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
