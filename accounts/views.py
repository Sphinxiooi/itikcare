import logging
import secrets

from django.conf import settings
from django.contrib import messages
from django.contrib.auth import login
from django.contrib.auth.views import LoginView
from django.core.management import call_command
from django.db import transaction
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils.decorators import method_decorator
from django_ratelimit.decorators import ratelimit

from farm.weather import geocode_address

from . import google_oauth
from .forms import SignupForm
from .models import User

logger = logging.getLogger(__name__)


class RateLimitedLoginView(LoginView):
    """LoginView with a per-IP attempt cap, for standard brute-force protection on a
    publicly reachable login page. 10/m is generous for a genuine farmer mistyping a
    password a few times, tight enough to make password-guessing impractical.

    Uses django_ratelimit's default cache backend (Django's CACHES, LocMemCache unless
    configured) — correct for a single-process VM deployment; would need a shared cache
    (e.g. Redis) to stay accurate across multiple gunicorn workers, since each worker
    process would otherwise keep its own separate counter.
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
            login(request, user)
            _bootstrap_train(request, user)
            return redirect("dashboard")
    else:
        form = SignupForm()
    return render(request, "registration/signup.html", {"form": form})


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
    """
    if not settings.GOOGLE_OAUTH_CLIENT_ID:
        messages.error(request, "Google sign-in isn't available right now.")
        return redirect("login")

    state = secrets.token_urlsafe(32)
    request.session["google_oauth_state"] = state
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
    state = request.GET.get("state")
    code = request.GET.get("code")
    if not code or not state or state != expected_state:
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

    login(request, user)
    if is_new_user:
        _bootstrap_train(request, user)
    return redirect("dashboard")
