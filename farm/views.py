import logging
from datetime import date

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db import IntegrityError, transaction
from django.db.models import Max
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_GET, require_POST

from forecasting.models import Forecast
from forecasting.services import (
    ModelNotTrainedError,
    generate_forecast,
    logging_gap_caution_for_log,
    readiness_for_log,
    trigger_retrain,
)

from itikcare.feedback import success_with_next

from .forms import DailyLogEditForm, DailyLogForm, FlockRegisterForm, FlockResumeCagingForm
from .models import AUDITED_FIELDS, DailyLog, DailyLogEdit, Flock
from .services import (
    RECORDS_CHART_MIN_LOGS,
    build_records_chart_data,
    caging_period_for_new_log,
    build_records_summary,
    current_flock_age_weeks,
    detect_daily_log_anomalies,
    get_active_flock,
    get_effective_coordinates,
    operational_today,
    recompute_caging_period,
)
from .weather import fetch_historical_weather
from django.utils.formats import date_format
from django.utils.translation import gettext as _

logger = logging.getLogger(__name__)


# ---------- daily log entry ----------
@login_required
def log_daily_data(request):
    """Create a DailyLog: validate, show a confirmation screen, then save (and forecast if dated today)."""

    active_flock = get_active_flock(request.user)
    if active_flock is None:
        messages.error(request, _("No active flock exists yet. Register your flock before logging daily data."))
        return redirect("flock_profile")
    if not active_flock.is_caged:
        messages.error(request, _("This flock is currently free-range in the field. Mark it as caged from Flock Profile before logging daily data."))
        return redirect("flock_profile")

    previous_log = DailyLog.objects.filter(flock=active_flock).order_by("-date").first()
    is_first_entry = previous_log is None

    if request.method == "POST":
        form = DailyLogForm(request.POST, active_flock=active_flock)
        editing = request.POST.get("edit") == "1"
        if not editing and form.is_valid():
            new_date = form.cleaned_data["date"]
            if DailyLog.objects.filter(flock=active_flock, date=new_date).exists():
                form.add_error("date", "A record for this date already exists — edit it from Farm Records instead.")
            elif request.POST.get("confirmed") != "1":
                # First pass: show the confirmation screen (with anomaly warnings), don't save yet
                anomaly_warnings = detect_daily_log_anomalies(active_flock, form.cleaned_data)
                context = {
                    "active_nav": "log_daily_data",
                    "form": form,
                    "confirm_mode": True,
                    "anomaly_warnings": anomaly_warnings,
                }
                return render(request, "farm/log_daily_data.html", context)
            else:
                daily_log = form.save(commit=False)
                daily_log.flock = active_flock
                # Caging period is never farmer-entered
                daily_log.caging_period = caging_period_for_new_log(active_flock, request.user)
                is_new_period = not is_first_entry and daily_log.caging_period != previous_log.caging_period
                daily_log.recorded_by = request.user
                try:
                    # Savepoint: a double-submit for the same date hits the unique constraint
                    with transaction.atomic():
                        daily_log.save()
                except IntegrityError:
                    form.add_error("date", "A record for this date already exists — edit it from Farm Records instead.")
                else:
                    if (
                        active_flock.pending_flock_size is not None
                        or active_flock.pending_flock_age_weeks is not None
                        or active_flock.pending_feed_intake_kg is not None
                        or active_flock.starts_new_caging_period
                    ):
                        # Clear staged values only now that a real DailyLog used them
                        active_flock.pending_flock_size = None
                        active_flock.pending_flock_age_weeks = None
                        active_flock.pending_feed_intake_kg = None
                        active_flock.starts_new_caging_period = False
                        active_flock.save(
                            update_fields=[
                                "pending_flock_size",
                                "pending_flock_age_weeks",
                                "pending_feed_intake_kg",
                                "starts_new_caging_period",
                            ]
                        )
                    # The success card is queued at the end (below) so its "what happens
                    # next" list reflects what actually happened -- a forecast that
                    # failed must not be promised.
                    next_steps = [
                        _("It's in Farm Records now. If you spot a typo, edit it there — every change is logged."),
                    ]
                    if new_date == operational_today():
                        try:
                            forecast = generate_forecast(daily_log)
                            if not forecast.has_prediction:
                                # Warm-up: recommendations only, yield prediction withheld
                                next_steps.insert(0, _("Recommendations were refreshed. Egg-yield forecasts start once you've logged enough days."))
                                warmup = readiness_for_log(daily_log)
                                messages.info(
                                    request,
                                    _(
                                        "%(so_far)s/%(needed)s days logged. Egg yield "
                                        "forecasts start once there's enough of your own data. "
                                        "Recommendations are ready now."
                                    ) % {"so_far": warmup.logs_so_far, "needed": warmup.logs_needed},
                                )
                            else:
                                next_steps.insert(0, _("Your forecast and recommendations were refreshed from this entry."))
                                gap_caution = logging_gap_caution_for_log(daily_log)
                                if gap_caution is not None:
                                    messages.warning(
                                        request,
                                        _(
                                            "No logs for %(days)s days before this entry, so this "
                                            "forecast may be less accurate until you've logged a few "
                                            "days in a row again."
                                        ) % {"days": gap_caution.missed_days},
                                    )
                        except ModelNotTrainedError:
                            messages.warning(
                                request,
                                _("No trained forecasting model exists yet, so no forecast was "
                                "generated for this entry. Ask an admin to run the training command."),
                            )
                        except Exception:
                            logger.exception("Forecast generation failed for DailyLog id=%s", daily_log.pk)
                            messages.warning(
                                request,
                                _("Your data was saved, but the forecast could not be generated this time."),
                            )
                    else:
                        # Backdated entry: saved as history, no forecast of its own
                        messages.info(
                            request,
                            _("This entry backfills a past date, so it won't generate its own "
                            "forecast — it's still saved as history and will feed into future "
                            "predictions."),
                        )
                    if is_new_period:
                        # Previous caging period is closed, so retrain on the new segment
                        trigger_retrain("caging_period_closed", active_flock.owner_id)
                        messages.info(request, _("New caging period detected — model retraining triggered."))
                    success_with_next(request, _("Daily data saved."), next_steps)
                    return redirect("dashboard")
    else:
        initial = {}
        if previous_log is not None:
            initial["flock_age_weeks"] = current_flock_age_weeks(previous_log)
            initial["flock_size"] = previous_log.flock_size
        if active_flock.pending_flock_size is not None:
            # Staged count (resume caging) is more current than the last logged size
            initial["flock_size"] = active_flock.pending_flock_size
        if active_flock.pending_flock_age_weeks is not None:
            # Staged at registration; fills the age on a flock's first entry
            initial["flock_age_weeks"] = active_flock.pending_flock_age_weeks
        if active_flock.pending_feed_intake_kg is not None:
            initial["feed_intake_kg"] = active_flock.pending_feed_intake_kg

        form = DailyLogForm(initial=initial, active_flock=active_flock)

    context = {"active_nav": "log_daily_data", "form": form}
    return render(request, "farm/log_daily_data.html", context)


# ---------- weather suggestion (JSON) ----------
@login_required
@require_GET
def weather_for_date(request):
    """Return suggested temperature/humidity for ?date=YYYY-MM-DD as JSON, or null if unavailable."""
    try:
        target_date = date.fromisoformat(request.GET.get("date", ""))
    except ValueError:
        return JsonResponse(None, safe=False)

    latitude, longitude = get_effective_coordinates(request.user)
    weather = fetch_historical_weather(target_date, latitude, longitude)
    return JsonResponse(weather, safe=False)


# ---------- farm records ----------
@login_required
def farm_records(request):
    """List DailyLogs for a flock (default: active flock), filtered by month, with charts/summary."""

    owner_flocks = list(Flock.objects.filter(owner=request.user).order_by("-generation_number"))
    flock_choices = {
        str(f.id): (_("Flock #%(number)s (active)") if f.is_active else _("Flock #%(number)s (retired)"))
        % {"number": f.generation_number}
        for f in owner_flocks
    }
    flocks_by_id = {str(f.id): f for f in owner_flocks}

    active_flock = get_active_flock(request.user)
    default_flock = active_flock or (owner_flocks[0] if owner_flocks else None)
    selected_flock = flocks_by_id.get(request.GET.get("flock"), default_flock)
    selected_flock_id = str(selected_flock.id) if selected_flock else ""

    # select_related avoids one extra query per row (template reads log.flock.is_active)
    logs = (
        DailyLog.objects.filter(flock=selected_flock).order_by("-date")
        .select_related("flock").prefetch_related("edits")
        if selected_flock else DailyLog.objects.none()
    )

    month_values = (
        DailyLog.objects.filter(flock=selected_flock).dates("date", "month", order="DESC")
        if selected_flock else []
    )
    month_choices = {"all": _("All months"), **{d.strftime("%Y-%m"): date_format(d, "F Y") for d in month_values}}

    selected_month = request.GET.get("month", "all")
    if selected_month not in month_choices:
        selected_month = "all"
    if selected_month != "all":
        year, month = (int(part) for part in selected_month.split("-"))
        logs = logs.filter(date__year=year, date__month=month)

    # Evaluate once; it is inspected several times below
    logs = list(logs)

    # Previous month's logs (for month-over-month deltas) only when a month is selected
    previous_month_logs = []
    if selected_flock and selected_month != "all":
        year, month = (int(part) for part in selected_month.split("-"))
        prev_year, prev_month = (year - 1, 12) if month == 1 else (year, month - 1)
        previous_month_logs = list(
            DailyLog.objects.filter(flock=selected_flock, date__year=prev_year, date__month=prev_month)
        )

    context = {
        "active_nav": "records",
        "logs": logs,
        "flock_choices": flock_choices,
        "selected_flock_id": selected_flock_id,
        "month_choices": month_choices,
        "selected_month": selected_month,
        "records_charts_available": len(logs) >= RECORDS_CHART_MIN_LOGS,
        # Lets the empty state pick the right next step (register / un-cage / log).
        "active_flock": active_flock,
        "flock_is_caged": bool(active_flock and active_flock.is_caged),
    }
    if context["records_charts_available"]:
        context.update(build_records_chart_data(logs))
        context.update(build_records_summary(logs, previous_month_logs))
    return render(request, "farm/farm_records.html", context)


# ---------- edit a record (audited) ----------
@login_required
def farm_record_edit(request, pk):
    """Edit a DailyLog and write one DailyLogEdit audit row per changed field."""

    daily_log = get_object_or_404(DailyLog, pk=pk, flock__owner=request.user)
    if daily_log.is_locked:
        messages.error(
            request,
            _("This record was used to train a forecasting model and can no longer be edited or deleted."),
        )
        return redirect("farm_records")
    if not daily_log.flock.is_active:
        messages.error(
            request,
            _("This record belongs to a retired flock and is read-only."),
        )
        return redirect("farm_records")
    # Snapshot old values first: is_valid() already writes new values onto the instance
    old_values = {field_name: getattr(daily_log, field_name) for field_name in AUDITED_FIELDS}

    if request.method == "POST":
        form = DailyLogEditForm(request.POST, instance=daily_log)
        if form.is_valid():
            # The form can't enforce unique (flock, date) itself, so check it here
            new_date = form.cleaned_data["date"]
            if DailyLog.objects.filter(flock=daily_log.flock, date=new_date).exclude(pk=daily_log.pk).exists():
                form.add_error("date", _("Another record already exists for this date — edit that one instead."))

        if form.is_valid():
            changes = []
            for field_name in AUDITED_FIELDS:
                old_value = old_values[field_name]
                new_value = form.cleaned_data[field_name]
                if old_value != new_value:
                    changes.append((field_name, old_value, new_value))

            try:
                # One savepoint: no audit rows for an update that didn't take
                with transaction.atomic():
                    updated_log = form.save()
                    if updated_log.date != old_values["date"]:
                        # Date moved, so its caging period may no longer fit
                        recompute_caging_period(updated_log)
                    for field_name, old_value, new_value in changes:
                        DailyLogEdit.objects.create(
                            daily_log=updated_log,
                            field_name=field_name,
                            old_value=str(old_value),
                            new_value=str(new_value),
                            changed_by=request.user,
                        )
            except IntegrityError:
                form.add_error("date", _("Another record already exists for this date — edit that one instead."))
            else:
                if changes:
                    success_with_next(
                        request,
                        _("Record updated (%(count)d field(s) changed).") % {"count": len(changes)},
                        [_("The old and new values are kept in this record's edit history.")],
                    )
                else:
                    success_with_next(request, _("No changes made."), [_("The record is exactly as it was.")])
                return redirect("farm_records")
    else:
        form = DailyLogEditForm(instance=daily_log)

    context = {"active_nav": "records", "form": form, "daily_log": daily_log}
    return render(request, "farm/farm_record_edit.html", context)


# ---------- delete a record ----------
@login_required
def farm_record_delete(request, pk):
    """Delete a DailyLog (after a confirm page) along with its same-day Forecast."""

    daily_log = get_object_or_404(DailyLog, pk=pk, flock__owner=request.user)
    if daily_log.is_locked:
        messages.error(
            request,
            _("This record was used to train a forecasting model and can no longer be edited or deleted."),
        )
        return redirect("farm_records")
    if not daily_log.flock.is_active:
        messages.error(
            request,
            _("This record belongs to a retired flock and is read-only."),
        )
        return redirect("farm_records")

    if request.method == "POST":
        log_date = daily_log.date
        flock = daily_log.flock
        daily_log.delete()
        Forecast.objects.filter(flock=flock, forecast_date=log_date).delete()
        messages.success(request, _("Record for %(date)s deleted.") % {"date": log_date})
        return redirect("farm_records")

    context = {"active_nav": "records", "daily_log": daily_log}
    return render(request, "farm/farm_record_delete_confirm.html", context)


# ---------- flock profile ----------
def flock_profile_context(user):
    """Active-flock context shared by flock_profile and account_settings."""
    active_flock = get_active_flock(user)

    if active_flock is None:
        return {"active_flock": None, "form": FlockRegisterForm()}

    latest_log = DailyLog.objects.filter(flock=active_flock).order_by("-date").first()
    resume_form = None
    if not active_flock.is_caged:
        resume_initial = {"flock_size": latest_log.flock_size} if latest_log else {}
        resume_form = FlockResumeCagingForm(initial=resume_initial)

    return {
        "active_flock": active_flock,
        "latest_log": latest_log,
        # Age projected to today (not the snapshot on latest_log); falls back to the staged age
        "current_age_weeks": current_flock_age_weeks(latest_log) or active_flock.pending_flock_age_weeks,
        "resume_form": resume_form,
    }


@login_required
def flock_profile(request):
    """Show the active flock's profile, or register the first/next flock if none is active."""

    template_name = (
        "farm/_flock_profile_panel.html" if request.GET.get("partial") == "1" else "farm/flock_profile.html"
    )
    active_flock = get_active_flock(request.user)

    if active_flock is None and request.method == "POST":
        form = FlockRegisterForm(request.POST)
        if form.is_valid():
            # Max() so a retired flock's generation number is never reused
            last_generation = Flock.objects.filter(owner=request.user).aggregate(
                Max("generation_number")
            )["generation_number__max"] or 0
            Flock.objects.create(
                owner=request.user,
                generation_number=last_generation + 1,
                started_on=form.cleaned_data["started_on"],
                pending_flock_size=form.cleaned_data["flock_size"],
                pending_flock_age_weeks=form.cleaned_data["flock_age_weeks"],
                pending_feed_intake_kg=form.cleaned_data["feed_intake_kg"],
            )
            success_with_next(
                request,
                _("Flock registered."),
                [_("Next, log today's data. Your starting flock size, age and feed are already pre-filled."),
                 _("Forecasts begin once you've logged enough days of your own data.")],
            )
            return redirect("flock_profile")
        context = {"active_nav": "flock_profile", "active_flock": None, "form": form}
        return render(request, template_name, context)

    context = {"active_nav": "flock_profile", **flock_profile_context(request.user)}
    return render(request, template_name, context)


# ---------- flock lifecycle ----------
@login_required
@require_POST
def flock_retire(request):
    """Retire the active flock and trigger retraining; the next flock is registered separately."""

    active_flock = get_active_flock(request.user)
    if active_flock is None:
        messages.error(request, _("No active flock to retire."))
        return redirect("flock_profile")

    active_flock.is_active = False
    active_flock.save(update_fields=["is_active"])
    # A whole generation of history is now complete
    trigger_retrain("flock_retired", active_flock.owner_id)
    messages.success(request, _("Flock retired. Register a new flock when you're ready to start the next generation."))
    messages.info(request, _("Model retraining has been triggered in the background."))
    return redirect("flock_profile")


@login_required
@require_POST
def toggle_caging_status(request):
    """Flip the active flock between caged and free-range (nothing is deleted)."""

    active_flock = get_active_flock(request.user)
    if active_flock is None:
        messages.error(request, _("No active flock to update."))
        return redirect("flock_profile")

    active_flock.is_caged = not active_flock.is_caged
    if active_flock.is_caged:
        # Back from free-range: next DailyLog opens a new caging period
        active_flock.starts_new_caging_period = True
    active_flock.save(update_fields=["is_caged", "starts_new_caging_period"])
    if active_flock.is_caged:
        messages.success(request, _("Flock marked as caged. Daily logging and forecasts have resumed."))
    else:
        messages.success(request, _("Flock marked as free-range. It's out in the field — logging and forecasts are paused until it's caged again."))
    return redirect("flock_profile")


@login_required
@require_POST
def resume_caging(request):
    """Mark a free-range flock as caged again and stage the confirmed duck count for the next log."""

    active_flock = get_active_flock(request.user)
    if active_flock is None or active_flock.is_caged:
        messages.error(request, _("No free-range flock to resume caging for."))
        return redirect("flock_profile")

    form = FlockResumeCagingForm(request.POST)
    if form.is_valid():
        active_flock.is_caged = True
        active_flock.pending_flock_size = form.cleaned_data["flock_size"]
        # Back from free-range: next DailyLog opens a new caging period
        active_flock.starts_new_caging_period = True
        active_flock.save(update_fields=["is_caged", "pending_flock_size", "starts_new_caging_period"])
        messages.success(
            request,
            _("Flock marked as caged with %(size)s ducks. Daily logging and forecasts have resumed.")
            % {"size": form.cleaned_data["flock_size"]},
        )
    else:
        messages.error(request, form.errors["flock_size"][0])
    return redirect("flock_profile")
