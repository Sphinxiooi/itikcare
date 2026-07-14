"""URL configuration for the itikcare project.

Login/logout/signup/password-reset are wired explicitly (rather than including the
full django.contrib.auth.urls) to keep the URL surface easy to reason about and to
attach this project's own styled forms/templates/rate limiting to each view.
"""
from django.contrib import admin
from django.contrib.auth import views as auth_views
from django.urls import include, path

from accounts import views as accounts_views
from accounts.forms import StyledAuthenticationForm, StyledPasswordResetForm, StyledSetPasswordForm

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
    path(
        'accounts/password-reset/',
        auth_views.PasswordResetView.as_view(
            form_class=StyledPasswordResetForm,
            template_name='registration/password_reset_form.html',
            email_template_name='registration/password_reset_email.html',
            subject_template_name='registration/password_reset_subject.txt',
        ),
        name='password_reset',
    ),
    path(
        'accounts/password-reset/done/',
        auth_views.PasswordResetDoneView.as_view(template_name='registration/password_reset_done.html'),
        name='password_reset_done',
    ),
    path(
        'accounts/reset/<uidb64>/<token>/',
        auth_views.PasswordResetConfirmView.as_view(
            form_class=StyledSetPasswordForm,
            template_name='registration/password_reset_confirm.html',
        ),
        name='password_reset_confirm',
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
