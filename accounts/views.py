import logging

from django.contrib import messages
from django.contrib.auth import login
from django.core.management import call_command
from django.db import transaction
from django.shortcuts import redirect, render

from farm.weather import geocode_address

from .forms import SignupForm

logger = logging.getLogger(__name__)


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
            return redirect("dashboard")
    else:
        form = SignupForm()
    return render(request, "registration/signup.html", {"form": form})
