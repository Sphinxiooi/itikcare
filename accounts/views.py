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
from .forms import (
    AccountDeletionForm,
    AccountSettingsForm,
    SignupForm,
    UsernameLookupForm,
    VerifyResetCodeForm,
)
from .models import PasswordResetCode, User

logger = logging.getLogger(__name__)


class RateLimitedLoginView(LoginView):
    """LoginView with a per-IP attempt cap, for standard brute-force protection on a
    publicly reachable login page. 10/m is generous for a genuine farmer mistyping a
    password a few times, tight enough to make password-guessing impractical.

    Uses django_ratelimit's default cache backend (Django's CACHES, LocMemCache unless
    DJANGO_SHARED_CACHE=True, see itikcare/settings.py). LocMemCache is accurate only for
    a single process; with several gunicorn workers set DJANGO_SHARED_CACHE=True so all
    of them share one counter instead of each keeping its own. Behind a reverse proxy,
    the client IP comes from accounts/ratelimit.py (RATELIMIT_IP_META_KEY).
    """

    @method_decorator(ratelimit(key="ip", rate="10/m", method="POST", block=True))
    def dispatch(self, request, *args, **kwargs):
        return super().dispatch(request, *args, **kwargs)


def _bootstrap_train(request, user):
    """Run the synchronous bootstrap training job a brand-new farmer needs before their
    first daily log can be forecast — see `signup`'s docstring for the full rationale.
    Shared by `signup` and `google_callback`, the two ways a new account gets created,
    so both run the identical bootstrap step rather than duplicating it.
    """
    try:
        call_command("train_forecast_model", owner_id=user.id, strict=True)
    except Exception:
        logger.exception("Bootstrap training failed for new user id=%s", user.id)
        messages.warning(
            request,
            "Your account is ready, but your starter forecasting model is "
            "still warming up — try logging your first daily entry again "
            "shortly if no forecast appears.",
        )


@ratelimit(key="ip", rate="5/d", method="POST", block=True)
@transaction.atomic
def signup(request):
    """Self-service farmer signup, then a synchronous bootstrap training run.

    The new farmer has zero DailyLogs at signup time, so `train_forecast_model
    --owner-id <id>` trains on exactly the foundation farmer's historical data (see
    that command's `_load_records`) — this is what "seeds every new farmer's model
    from the founding dataset" actually means in code.

    Run synchronously (call_command, not the async subprocess `trigger_retrain` used
    for routine retrains) and without --tune: this bootstrap train must finish before
    the redirect to the dashboard, because the farmer's very first `log_daily_data`
    submission calls `generate_forecast`, which needs this owner's model artifact to
    already exist. Firing it via `trigger_retrain`'s detached subprocess would leave a
    race — a farmer logging their first entry within seconds of signing up could beat
    the background process. Dropping --tune keeps the fixed-hyperparameter path fast
    enough to run inline in this request; the farmer's first *real* retrain (once they
    have their own data) goes through the normal async, tuned `trigger_retrain` path.

    Rate-limited at 5 POSTs/day/IP (see class docstring above for the same caveat on
    django_ratelimit's cache backend): this is the actual cost-exhaustion vector flagged
    during the deployment-readiness review — every submission runs a real training job,
    so an anonymous, unauthenticated endpoint that runs one on demand needs a hard cap
    before going public. Django_ratelimit returns HTTP 403 once the limit is hit.
    """
    if request.method == "POST":
        form = SignupForm(request.POST)
        if form.is_valid():
            user = form.save(commit=False)
            if user.address:
                coordinates = geocode_address(user.address)
                if coordinates is not None:
                    user.latitude, user.longitude = coordinates
                else:
                    messages.info(
                        request,
                        "We couldn't find that farm address, so weather suggestions "
                        "will use the default location for now.",
                    )
            user.save()
            # Skips authenticate() (the password was just set, not typed in to be
            # checked) -- with more than one AUTHENTICATION_BACKENDS configured,
            # login() needs to be told explicitly which one to attribute this session
            # to, same as google_callback below.
            login(request, user, backend="accounts.auth_backends.UsernameEmailOrFullNameBackend")
            _bootstrap_train(request, user)
            # A brand-new account has no Flock, and nothing can be logged or forecast
            # without one -- send them straight to the Register Flock form.
            messages.warning(request, "Welcome! Register your flock to start logging daily data.")
            return redirect("flock_profile")
    else:
        form = SignupForm()
    return render(request, "registration/signup.html", {"form": form})


@login_required
def account_settings(request):
    """Personal-data settings for the logged-in farmer: name, username, email, farm
    address, and profile picture (AccountSettingsForm) -- reachable from the header
    avatar (templates/base.html), and where a brand-new Google sign-in is redirected
    to finish setting up their account (see google_callback below), since that flow
    creates a User with no name or address at all.

    Same "full page vs ?partial=1" split as farm.views.flock_profile, whose flock-
    context this view also gathers (via flock_profile_context) so the same
    "farm/_flock_profile_panel.html" partial can render as this page's second box —
    see templates/account/_settings_panel.html.

    The edit form's own submit is intercepted client-side (templates/base.html) via
    the same X-Requested-With header the header avatar's modal already fetches with
    -- a normal redirect-on-save would otherwise blow away the modal with a full-page
    navigation. On an AJAX save this re-renders the partial in place (with a fresh,
    unbound form so it collapses back to view mode) and a just_saved flag the partial
    turns into a self-dismissing banner, instead of going through messages.success +
    redirect, which only ever surfaces on the next full-page render.
    """
    is_ajax = request.headers.get("X-Requested-With") == "XMLHttpRequest"
    template_name = (
        "account/_settings_panel.html" if request.GET.get("partial") == "1" or is_ajax else "account/settings.html"
    )

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
                            "We couldn't find that farm address, so weather "
                            "suggestions will use the default location for now.",
                        )
                else:
                    user.latitude, user.longitude = None, None
            user.save()
            if is_ajax:
                just_saved = True
                form = AccountSettingsForm(instance=request.user)
            else:
                messages.success(request, "Account settings updated.")
                return redirect("account_settings")
    else:
        form = AccountSettingsForm(instance=request.user)

    context = {
        "active_nav": "account_settings",
        "account_form": form,
        # Settings is a personal-info page (and the header avatar's quick-access
        # modal, templates/base.html) -- flock lifecycle actions (retire, toggle
        # caging, register) belong on the dedicated /flock/ page, not duplicated
        # here, so the shared partial renders a read-only summary + link instead.
        "compact": True,
        "just_saved": just_saved,
        **flock_profile_context(request.user),
    }
    return render(request, template_name, context)


@login_required
@require_POST
def delete_account(request):
    """Self-service account deletion, triggered from the "Danger Zone" card on
    Account Settings (templates/account/_settings_panel.html). This is a soft
    delete: it deactivates the account and scrubs personally-identifying fields
    rather than removing the row, so the farmer's Flock/DailyLog/Forecast/
    Recommendation history survives intact for the forecasting model's training
    data -- there's no real benefit to cascade-deleting that, only lost history.

    The foundation farmer (User.is_foundation_farmer) is exempt: their historical
    DailyLog data bootstraps every new farmer's starter model (see
    train_forecast_model's docstring), so deleting that one account would break
    onboarding for everyone else. Also enforced in the template, which doesn't
    render a working delete button for that account at all -- this check is the
    real gate.

    Beyond the fields a farmer would recognize as "their info" (email, name,
    avatar, address, coordinates), this also clears google_sub and blanks the
    password. Without that, a deactivated Google-linked account could still sign
    back in: google_callback resolves a user by google_sub and calls login()
    directly, bypassing the is_active check that authenticate() would normally
    apply for a plain password login. Clearing google_sub closes that path too,
    so "deactivated" actually holds for every sign-in method the account has.
    """
    user = request.user
    if user.is_foundation_farmer:
        messages.error(
            request,
            "This account can't be deleted — it seeds every new farmer's starter "
            "forecasting model.",
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
        user.first_name = ""
        user.last_name = ""
        user.avatar = None
        user.address = ""
        user.latitude = None
        user.longitude = None
        user.google_sub = None
        user.set_unusable_password()
        user.is_active = False
        user.save()

    logout(request)
    messages.info(request, "Your account has been deleted.")
    return redirect("login")


RESET_CODE_EXPIRY_MINUTES = 10
RESET_CODE_MAX_ATTEMPTS = 5


def _mask_email(email):
    """Show enough of an email (first 2 + last 2 characters of the local part) for a
    farmer to recognize their own address in request_reset_code's step-2 handoff,
    without fully exposing it to anyone who just guesses a username."""
    local, _, domain = email.partition("@")
    if len(local) <= 4:
        masked_local = local[0] + "•" * max(len(local) - 1, 1)
    else:
        masked_local = local[:2] + "•" * (len(local) - 4) + local[-2:]
    return f"{masked_local}@{domain}"


@ratelimit(key="ip", rate="10/h", method="POST", block=True)
def request_reset_code(request):
    """Step 1 of the password-reset flow: the farmer enters their username, not an
    email address -- many won't remember which one (if any) they signed up with, since
    email is optional at signup (see SignupForm's docstring above). If the account has
    an email on file, emails it a fresh 6-digit code and hands off to verify_reset_code.

    Replaces Django's built-in tokenized-link reset, which breaks in practice here: the
    link points at whatever host served the request (localhost in dev), meaningless
    once the farmer opens the email on a different device.

    Deliberately gives the same "if that username exists..." message for both an
    unknown username and one with no email on file, so neither response alone confirms
    an account exists -- reaching step 2 is the only signal that it does, which is the
    accepted trade-off recorded in this feature's plan doc.
    """
    if request.method == "POST":
        form = UsernameLookupForm(request.POST)
        if form.is_valid():
            username = form.cleaned_data["username"]
            user = User.objects.filter(username=username, is_active=True).first()
            if user is None:
                messages.info(
                    request,
                    "If that username exists and has an email on file, "
                    "we've sent it a verification code.",
                )
            elif not user.email:
                messages.info(
                    request,
                    "That account doesn't have an email on file, so it can't "
                    "self-service a reset — ask an admin to reset your password "
                    "from /admin/ instead.",
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
                        send_mail(
                            subject,
                            render_to_string(
                                "registration/password_reset_email.html",
                                {"user": user, "code": code},
                            ),
                            None,
                            [user.email],
                        )
                    except Exception:
                        logger.exception(
                            "Failed to send password-reset code to user id=%s", user.id
                        )
                        messages.warning(
                            request,
                            "We couldn't confirm the email sent just now — if no code "
                            "arrives shortly, request a new one.",
                        )
                request.session["password_reset_username"] = user.username
                return redirect("password_reset_verify")
    else:
        form = UsernameLookupForm()
    return render(request, "registration/password_reset_form.html", {"form": form})


@ratelimit(key="ip", rate="20/h", method="POST", block=True)
def verify_reset_code(request):
    """Step 2 of the password-reset flow: the farmer enters the 6-digit code just
    emailed (see request_reset_code above) plus a new password. A matching, unexpired
    code sets the new password immediately -- no separate confirmation link/page."""
    username = request.session.get("password_reset_username")
    user = User.objects.filter(username=username, is_active=True).first() if username else None
    if user is None:
        request.session.pop("password_reset_username", None)
        messages.info(request, "Start the password reset process again.")
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
                    messages.error(request, "That code has expired — request a new one.")
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
                            request, "Too many incorrect attempts — request a new code."
                        )
                        return redirect("password_reset")
                    reset_code.save(update_fields=["attempts"])
                    remaining = RESET_CODE_MAX_ATTEMPTS - reset_code.attempts
                    form.add_error("code", f"Incorrect code — {remaining} attempt(s) left.")
        if reset_succeeded:
            return redirect("password_reset_complete")
    else:
        form = VerifyResetCodeForm(user=user)
    return render(
        request,
        "registration/password_reset_confirm.html",
        {"form": form, "masked_email": masked_email},
    )


def _generate_unique_username(base):
    """Turn a Google account's email local-part into a free username, appending a
    numeric suffix if it's already taken — two farmers signing in with Google can share
    an email local-part (e.g. juan.delacruz@gmail.com vs juan.delacruz@yahoo.com)."""
    username = base or "farmer"
    candidate = username
    suffix = 1
    while User.objects.filter(username=candidate).exists():
        suffix += 1
        candidate = f"{username}{suffix}"
    return candidate


def google_login(request):
    """Redirect the farmer to Google's own consent screen. See accounts/google_oauth.py
    for why this is hand-rolled with `requests` rather than a library.

    `state` is a one-time random token stashed in the session and checked again in
    `google_callback` — standard OAuth2 protection against a forged callback request
    that didn't actually originate from this browser's own sign-in attempt.

    A `?next=` query param (added by login.html whenever Django's own login_required
    redirect sent the farmer here with one, same as the plain username/password form
    already honors) is likewise stashed in the session and consumed by google_callback
    once sign-in succeeds, so choosing "Sign in with Google" from a next-carrying login
    page returns the farmer to the page they actually wanted instead of always dumping
    them on the dashboard. Validated with url_has_allowed_host_and_scheme (same check
    Django's own LoginView applies to `next`) so a crafted `?next=` can't be used as an
    open redirect.
    """
    if not settings.GOOGLE_OAUTH_CLIENT_ID:
        messages.error(request, "Google sign-in isn't available right now.")
        return redirect("login")

    state = secrets.token_urlsafe(32)
    request.session["google_oauth_state"] = state

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
    """Handle Google's redirect back after the farmer approves (or cancels) sign-in.

    Resolves to a local User in this order (see accounts/google_oauth.py's
    fetch_google_account for exactly what Google hands back):

    1. `google_sub` already on file -> that user (a returning Google sign-in).
    2. No `google_sub` match, but Google reports email_verified=True and it matches an
       existing local account's email -> link this Google account to it. Only linking
       on a *verified* email is what makes this safe: Google won't vouch for an email
       its account holder doesn't own, so this can't be used to hijack another farmer's
       account by typing their email into a throwaway Google account.
    3. No match at all -> create a brand-new account, same "farmer role, no local
       password, synchronous bootstrap train" shape `signup` uses for a fresh signup.

    Rate-limited (10/h/IP) as defense in depth on top of Google's own consent screen,
    which is the primary abuse barrier here — see `signup`'s docstring for why the new-
    account path this can also take needs a cap at all (it runs a real training job).
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
        messages.error(request, "Google sign-in didn't complete — please try again.")
        return redirect("login")

    redirect_uri = request.build_absolute_uri(reverse("google_callback"))
    account = google_oauth.fetch_google_account(code, redirect_uri)
    if account is None:
        messages.error(request, "Google sign-in didn't complete — please try again.")
        return redirect("login")

    user = User.objects.filter(google_sub=account["sub"]).first()

    if user is None and account["email_verified"] and account["email"]:
        user = User.objects.filter(email__iexact=account["email"]).first()
        if user is not None:
            user.google_sub = account["sub"]
            user.save(update_fields=["google_sub"])

    is_new_user = user is None
    if is_new_user:
        with transaction.atomic():
            username_base = (account["email"] or "").split("@")[0]
            user = User(
                username=_generate_unique_username(username_base),
                email=account["email"] or "",
                google_sub=account["sub"],
            )
            user.set_unusable_password()
            user.save()

    # Google's own consent screen is the actual authentication here -- there's no
    # local password to authenticate() against, so the backend must be named
    # explicitly (see signup's identical login() call above).
    login(request, user, backend="accounts.auth_backends.UsernameEmailOrFullNameBackend")
    if is_new_user:
        _bootstrap_train(request, user)
        # A Google account is created with no name or address at all -- send them
        # straight to the same settings form a farmer would use to edit that data
        # later, instead of the dashboard, so it doesn't stay permanently blank.
        messages.info(
            request,
            "Welcome! Add your name and farm location so we can personalize your "
            "dashboard.",
        )
        messages.warning(request, "You also need to register your flock before you can log daily data.")
        return redirect("account_settings")
    return redirect(next_url) if next_url else redirect("dashboard")
