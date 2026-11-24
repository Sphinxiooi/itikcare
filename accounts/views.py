import logging
import secrets
from datetime import timedelta

from django.conf import settings
from django.contrib import messages
from django.contrib.auth import login, logout
from django.contrib.auth.decorators import login_required
from django.contrib.auth.hashers import check_password, make_password
from django.contrib.auth.views import LoginView
from django.core.mail import send_mail
from django.core.management import call_command
from django.db import transaction
from django.shortcuts import redirect, render
from django.template.loader import render_to_string
from django.urls import reverse
from django.utils import timezone
from django.utils.decorators import method_decorator
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.http import require_POST
from django_ratelimit.decorators import ratelimit

from farm.views import flock_profile_context
from farm.weather import geocode_address

from . import google_oauth
from .auth_backends import find_user_by_login_identifier
from .contact import generate_unique_username
from .forms import (
    AccountDeletionForm,
    AccountLookupForm,
    AccountSettingsForm,
    SignupForm,
    VerifyResetCodeForm,
)
from .models import PasswordResetCode, User
from django.utils.translation import gettext as _

logger = logging.getLogger(__name__)


# ---------- login & signup ----------
class RateLimitedLoginView(LoginView):
    """LoginView limited to 10 POST attempts per minute per IP (brute-force protection)."""

    @method_decorator(ratelimit(key="ip", rate="10/m", method="POST", block=True))
    def dispatch(self, request, *args, **kwargs):
        return super().dispatch(request, *args, **kwargs)


def _bootstrap_train(request, user):
    """Train a new farmer's starter model synchronously (used by signup and google_callback)."""
    try:
        call_command("train_forecast_model", owner_id=user.id, strict=True)
    except Exception:
        logger.exception("Bootstrap training failed for new user id=%s", user.id)
        messages.warning(
            request,
            _("Your account is ready, but your starter forecasting model is "
            "still warming up — try logging your first daily entry again "
            "shortly if no forecast appears."),
        )


@ratelimit(key="ip", rate="5/d", method="POST", block=True)
@transaction.atomic
def signup(request):
    """Farmer signup (5/day/IP), then a synchronous untuned bootstrap training run.

    Runs inline so the model exists before the farmer's first daily log is forecast.
    """
    if request.method == "POST":
        form = SignupForm(request.POST)
        if form.is_valid():
            user = form.save(commit=False)
            # The privacy consent checkbox is required, so it was ticked
            user.privacy_consented_at = timezone.now()
            if user.address:
                coordinates = geocode_address(user.address)
                if coordinates is not None:
                    user.latitude, user.longitude = coordinates
                else:
                    messages.info(
                        request,
                        _("We couldn't find that farm address, so weather suggestions "
                        "will use the default location for now."),
                    )
            user.save()
            # Name the backend explicitly (several are configured)
            login(request, user, backend="accounts.auth_backends.UsernameEmailOrFullNameBackend")
            _bootstrap_train(request, user)
            # No flock yet: send them to register one
            messages.warning(request, _("Welcome! Register your flock to start logging daily data."))
            return redirect("flock_profile")
    else:
        form = SignupForm()
    return render(request, "registration/signup.html", {"form": form})


# ---------- account settings ----------
@login_required
def account_settings(request):
    """Personal-data settings (name, email, address, avatar); AJAX saves re-render the panel in place."""
    is_ajax = request.headers.get("X-Requested-With") == "XMLHttpRequest"
    template_name = "account/_settings_panel.html" if is_ajax else "account/settings.html"

    just_saved = False
    if request.method == "POST":
        form = AccountSettingsForm(request.POST, request.FILES, instance=request.user)
        if form.is_valid():
            address_changed = "address" in form.changed_data
            user = form.save(commit=False)
            if address_changed:
                if user.address:
                    coordinates = geocode_address(user.address)
                    if coordinates is not None:
                        user.latitude, user.longitude = coordinates
                    else:
                        messages.info(
                            request,
                            _("We couldn't find that farm address, so weather "
                            "suggestions will use the default location for now."),
                        )
                else:
                    user.latitude, user.longitude = None, None
            user.save()
            if is_ajax:
                just_saved = True
                form = AccountSettingsForm(instance=request.user)
            else:
                messages.success(request, _("Account settings updated."))
                return redirect("account_settings")
    else:
        form = AccountSettingsForm(instance=request.user)

    context = {
        "active_nav": "account_settings",
        "account_form": form,
        # Read-only flock summary; flock actions live on /flock/
        "compact": True,
        "just_saved": just_saved,
        **flock_profile_context(request.user),
    }
    return render(request, template_name, context)


@login_required
@require_POST
def delete_account(request):
    """Soft-delete the account: deactivate it and scrub personal data, keeping farm history.

    The foundation farmer can't be deleted (their data seeds every new farmer's model).
    """
    user = request.user
    if user.is_foundation_farmer:
        messages.error(
            request,
            _("This account can't be deleted — it seeds every new farmer's starter "
            "forecasting model."),
        )
        return redirect("account_settings")

    form = AccountDeletionForm(request.POST, user=user)
    if not form.is_valid():
        messages.error(request, form.errors["confirmation"][0])
        return redirect("account_settings")

    with transaction.atomic():
        if user.avatar:
            user.avatar.delete(save=False)
        user.email = ""
        user.phone_number = None
        user.first_name = ""
        user.last_name = ""
        user.avatar = None
        user.address = ""
        user.latitude = None
        user.longitude = None
        # Clearing google_sub and the password blocks every sign-in method
        user.google_sub = None
        user.set_unusable_password()
        user.is_active = False
        user.save()

    logout(request)
    messages.info(request, _("Your account has been deleted."))
    return redirect("login")


# ---------- password reset (emailed 6-digit code) ----------
RESET_CODE_EXPIRY_MINUTES = 10
RESET_CODE_MAX_ATTEMPTS = 5


def _mask_email(email):
    """Partially hide an email (first 2 + last 2 characters of the local part)."""
    local, _, domain = email.partition("@")
    if len(local) <= 4:
        masked_local = local[0] + "•" * max(len(local) - 1, 1)
    else:
        masked_local = local[:2] + "•" * (len(local) - 4) + local[-2:]
    return f"{masked_local}@{domain}"


@ratelimit(key="ip", rate="10/h", method="POST", block=True)
def request_reset_code(request):
    """Step 1: look up the account by name/email/phone and email a 6-digit code.

    Unknown accounts and accounts without an email get the same kind of message,
    so the response doesn't confirm an account exists.
    """
    if request.method == "POST":
        form = AccountLookupForm(request.POST)
        if form.is_valid():
            user = find_user_by_login_identifier(form.cleaned_data["username"])
            if user is not None and not user.is_active:
                user = None
            if user is None:
                messages.info(
                    request,
                    _("If that account exists and has an email on file, "
                    "we've sent it a verification code."),
                )
            elif not user.email:
                messages.info(
                    request,
                    _("That account doesn't have an email on file, so it can't "
                    "self-service a reset — ask an admin to reset your password "
                    "from /admin/ instead."),
                )
            else:
                with transaction.atomic():
                    PasswordResetCode.objects.filter(
                        user=user, consumed_at__isnull=True
                    ).delete()
                    code = f"{secrets.randbelow(1_000_000):06d}"
                    PasswordResetCode.objects.create(
                        user=user,
                        code_hash=make_password(code),
                        expires_at=timezone.now()
                        + timedelta(minutes=RESET_CODE_EXPIRY_MINUTES),
                    )
                    try:
                        subject = "".join(
                            render_to_string("registration/password_reset_subject.txt").splitlines()
                        )
                        # Plain-text body plus an HTML alternative
                        email_context = {"user": user, "code": code}
                        send_mail(
                            subject,
                            render_to_string("registration/password_reset_email.txt", email_context),
                            None,
                            [user.email],
                            html_message=render_to_string(
                                "registration/password_reset_email.html", email_context
                            ),
                        )
                    except Exception:
                        logger.exception(
                            "Failed to send password-reset code to user id=%s", user.id
                        )
                        messages.warning(
                            request,
                            _("We couldn't confirm the email sent just now — if no code "
                            "arrives shortly, request a new one."),
                        )
                request.session["password_reset_username"] = user.username
                return redirect("password_reset_verify")
    else:
        form = AccountLookupForm()
    return render(request, "registration/password_reset_form.html", {"form": form})


@ratelimit(key="ip", rate="20/h", method="POST", block=True)
def verify_reset_code(request):
    """Step 2: check the emailed code and set the new password."""
    username = request.session.get("password_reset_username")
    user = User.objects.filter(username=username, is_active=True).first() if username else None
    if user is None:
        request.session.pop("password_reset_username", None)
        messages.info(request, _("Start the password reset process again."))
        return redirect("password_reset")

    masked_email = _mask_email(user.email)

    if request.method == "POST":
        form = VerifyResetCodeForm(user=user, data=request.POST)
        reset_succeeded = False
        if form.is_valid():
            with transaction.atomic():
                reset_code = (
                    PasswordResetCode.objects.select_for_update()
                    .filter(user=user, consumed_at__isnull=True, expires_at__gt=timezone.now())
                    .order_by("-created_at")
                    .first()
                )
                if reset_code is None:
                    request.session.pop("password_reset_username", None)
                    messages.error(request, _("That code has expired — request a new one."))
                    return redirect("password_reset")

                if check_password(form.cleaned_data["code"], reset_code.code_hash):
                    form.save()
                    reset_code.consumed_at = timezone.now()
                    reset_code.save(update_fields=["consumed_at"])
                    request.session.pop("password_reset_username", None)
                    reset_succeeded = True
                else:
                    reset_code.attempts += 1
                    if reset_code.attempts >= RESET_CODE_MAX_ATTEMPTS:
                        reset_code.delete()
                        request.session.pop("password_reset_username", None)
                        messages.error(
                            request, _("Too many incorrect attempts — request a new code.")
                        )
                        return redirect("password_reset")
                    reset_code.save(update_fields=["attempts"])
                    remaining = RESET_CODE_MAX_ATTEMPTS - reset_code.attempts
                    form.add_error("code", _("Incorrect code — %(remaining)s attempt(s) left.") % {"remaining": remaining})
        if reset_succeeded:
            return redirect("password_reset_complete")
    else:
        form = VerifyResetCodeForm(user=user)
    return render(
        request,
        "registration/password_reset_confirm.html",
        {"form": form, "masked_email": masked_email},
    )


# ---------- Google sign-in ----------
def google_login(request):
    """Send the farmer to Google's consent screen (state token + validated ?next= kept in the session)."""
    if not settings.GOOGLE_OAUTH_CLIENT_ID:
        messages.error(request, _("Google sign-in isn't available right now."))
        return redirect("login")

    # One-time token checked again in google_callback
    state = secrets.token_urlsafe(32)
    request.session["google_oauth_state"] = state

    # Only same-host ?next= values are kept (prevents open redirects)
    next_url = request.GET.get("next")
    if next_url and url_has_allowed_host_and_scheme(
        next_url, allowed_hosts={request.get_host()}, require_https=request.is_secure()
    ):
        request.session["google_oauth_next"] = next_url
    else:
        request.session.pop("google_oauth_next", None)

    redirect_uri = request.build_absolute_uri(reverse("google_callback"))
    return redirect(google_oauth.build_authorization_url(redirect_uri, state))


@ratelimit(key="ip", rate="10/h", method="GET", block=True)
def google_callback(request):
    """Handle Google's redirect: sign in by google_sub, link by verified email, or create a new account.

    Deactivated accounts are always blocked (login() here skips the is_active check).
    """
    expected_state = request.session.pop("google_oauth_state", None)
    next_url = request.session.pop("google_oauth_next", None)
    state = request.GET.get("state")
    code = request.GET.get("code")
    if not code or not state or state != expected_state:
        logger.warning(
            "Google sign-in state check failed: code_present=%s state_present=%s "
            "state_matches=%s",
            bool(code), bool(state), state == expected_state,
        )
        messages.error(request, _("Google sign-in didn't complete — please try again."))
        return redirect("login")

    redirect_uri = request.build_absolute_uri(reverse("google_callback"))
    account = google_oauth.fetch_google_account(code, redirect_uri)
    if account is None:
        messages.error(request, _("Google sign-in didn't complete — please try again."))
        return redirect("login")

    # Not filtered by is_active: an inactive match must be blocked below, not treated as "no match"
    user = User.objects.filter(google_sub=account["sub"]).first()

    # Link to an existing account only by a Google-verified email
    if user is None and account["email_verified"] and account["email"]:
        user = User.objects.filter(email__iexact=account["email"]).first()
        if user is not None and user.is_active:
            user.google_sub = account["sub"]
            user.save(update_fields=["google_sub"])

    if user is not None and not user.is_active:
        logger.warning("Google sign-in blocked for deactivated user id=%s", user.id)
        messages.error(
            request,
            _("This account has been deactivated. Contact an admin if you believe "
            "this is a mistake."),
        )
        return redirect("login")

    is_new_user = user is None
    if is_new_user:
        with transaction.atomic():
            username_base = (account["email"] or "").split("@")[0]
            user = User(
                username=generate_unique_username(username_base),
                email=account["email"] or "",
                google_sub=account["sub"],
                # Same privacy consent line as the signup form
                privacy_consented_at=timezone.now(),
            )
            user.set_unusable_password()
            user.save()

    # Google already authenticated them; name the backend explicitly
    login(request, user, backend="accounts.auth_backends.UsernameEmailOrFullNameBackend")
    if is_new_user:
        _bootstrap_train(request, user)
        # New Google accounts have no name/address, so send them to settings
        messages.info(
            request,
            _("Welcome! Add your name and farm location so we can personalize your "
            "dashboard."),
        )
        messages.warning(request, _("You also need to register your flock before you can log daily data."))
        return redirect("account_settings")
    return redirect(next_url) if next_url else redirect("dashboard")
