import logging
from datetime import date, timedelta

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db.models import Max
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from forecasting.models import Forecast
from forecasting.services import ModelNotTrainedError, generate_forecast, trigger_retrain

from .forms import DailyLogEditForm, DailyLogForm, FlockRegisterForm, FlockResumeCagingForm
from .models import AUDITED_FIELDS, DailyLog, DailyLogEdit, Flock
from .services import (
    assign_caging_periods,
    current_flock_age_weeks,
    detect_daily_log_anomalies,
    get_active_flock,
    get_effective_coordinates,
)
from .weather import fetch_current_weather

logger = logging.getLogger(__name__)


@login_required
def log_daily_data(request):
    """Create today's DailyLog entry.

    flock_size is pre-filled from the active flock's most recent DailyLog (see
    forms.DailyLogForm docstring) but is a normal, always-editable form field, so the
    farmer can adjust it up or down to reflect ducks lost/dead or added that day.
    flock_age_weeks is likewise pre-filled but advanced by however many calendar weeks
    have passed since that previous log, not carried forward flat — the ducks keep
    aging during a free-range gap even though nothing gets logged during it
    (itikcare-spec.md section 10), so a flock logged at 94 weeks that comes back into
    caging 6 weeks later should be pre-filled at 100, not still 94. caging_period is
    never farmer-entered: it continues the previous log's value, unless the gap since
    that log is long enough to imply a free-range-then-recage cycle happened in
    between (CAGING_PERIOD_GAP_DAYS).

    temperature_c and humidity_pct are similarly best-effort pre-filled, from a live
    weather API lookup at the farm's fixed coordinates (see weather.fetch_current_weather),
    whenever that succeeds — always editable, and left blank exactly as before if the
    lookup isn't configured or fails.

    A first valid submission never saves immediately: it renders a read-only
    confirmation screen (services.detect_daily_log_anomalies checked against this
    flock's own history, e.g. an egg count far above its usual average) so the farmer
    gets one last look before the entry is written (and, if it's dated today, a
    forecast generated from it). Only a second submission carrying confirmed=1
    actually saves. Clicking "Edit" from that screen (edit=1) returns to the normal
    editable form without re-validating.

    A backdated entry (backfilling a missed past day — see DailyLogForm.clean_date)
    is saved the same way but never generates its own Forecast: a same-day nowcast
    for a day that has already fully happened isn't actionable, and forecast_date
    is unique per flock+date, so generating one here would silently clash with
    whatever forecast already covers that historical date. It's still fed into the
    database as ordinary history, though — the next real (today-dated) forecast's
    lag1/roll3 features look at recent DailyLogs by date regardless of when each
    one was actually entered, so a backfilled day counts exactly the same as one
    logged on time.
    """

    active_flock = get_active_flock(request.user)
    if active_flock is None:
        messages.error(request, "No active flock exists yet. Create one from Flock Profile before logging daily data.")
        return redirect("dashboard")
    if not active_flock.is_caged:
        messages.error(request, "This flock is currently free-range in the field. Mark it as caged from Flock Profile before logging daily data.")
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
                # First successful validation pass — hold off on saving and show a
                # confirmation screen instead (with any anomaly warnings attached), so
                # the farmer gets one last look before the entry is written and a new
                # forecast is generated from it. Only a resubmission carrying
                # confirmed=1 (the confirm screen's own form) reaches the save below.
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
                # assign_caging_periods (farm/services.py) is the single source of truth
                # for this rule, shared with the bulk CSV import view — a flock's very
                # first entry always starts a new period (no prior date to gap-check
                # against, and it continues this owner's overall counter across flock
                # retirements per itikcare-spec.md section 10); later entries compare
                # against the previous log's date.
                daily_log.caging_period = assign_caging_periods(active_flock, request.user, [new_date])[0]
                is_new_period = not is_first_entry and daily_log.caging_period != previous_log.caging_period
                daily_log.recorded_by = request.user
                daily_log.save()
                if (
                    active_flock.pending_flock_size is not None
                    or active_flock.pending_flock_age_weeks is not None
                    or active_flock.pending_feed_intake_kg is not None
                ):
                    # Consumed only now that it's actually backed a real DailyLog, so an
                    # abandoned form (farmer navigates away without logging) doesn't
                    # silently lose the values they confirmed at resume/registration time.
                    active_flock.pending_flock_size = None
                    active_flock.pending_flock_age_weeks = None
                    active_flock.pending_feed_intake_kg = None
                    active_flock.save(
                        update_fields=["pending_flock_size", "pending_flock_age_weeks", "pending_feed_intake_kg"]
                    )
                messages.success(request, "Daily data saved.")
                if new_date == date.today():
                    try:
                        generate_forecast(daily_log)
                    except ModelNotTrainedError:
                        messages.warning(
                            request,
                            "No trained forecasting model exists yet, so no forecast was "
                            "generated for this entry. Ask an admin to run the training command.",
                        )
                    except Exception:
                        logger.exception("Forecast generation failed for DailyLog id=%s", daily_log.pk)
                        messages.warning(
                            request,
                            "Your data was saved, but the forecast could not be generated this time.",
                        )
                else:
                    # Backfilling a missed past day: still saved as ordinary history (see
                    # the log_daily_data docstring), just never gets its own forecast.
                    messages.info(
                        request,
                        "This entry backfills a past date, so it won't generate its own "
                        "forecast — it's still saved as history and will feed into future "
                        "predictions.",
                    )
                if is_new_period:
                    # The previous caging period just closed with this entry's gap — a
                    # complete new segment of training data now exists (itikcare-spec.md
                    # section 5's "rolling retraining as new data comes in").
                    trigger_retrain("caging_period_closed", active_flock.owner_id)
                    messages.info(request, "New caging period detected — model retraining triggered.")
                return redirect("dashboard")
    else:
        initial = {}
        if previous_log is not None:
            initial["flock_age_weeks"] = current_flock_age_weeks(previous_log)
            initial["flock_size"] = previous_log.flock_size
        if active_flock.pending_flock_size is not None:
            # Overrides previous_log.flock_size (or fills it in on a first-ever entry)
            # with the count the farmer confirmed when resuming caging, since that's
            # more current than whatever was last logged before the free-range gap.
            initial["flock_size"] = active_flock.pending_flock_size
        if active_flock.pending_flock_age_weeks is not None:
            # Set only at registration (views.flock_profile) — fills in
            # flock_age_weeks on a flock's very first entry, which otherwise has no
            # previous_log to derive it from.
            initial["flock_age_weeks"] = active_flock.pending_flock_age_weeks
        if active_flock.pending_feed_intake_kg is not None:
            initial["feed_intake_kg"] = active_flock.pending_feed_intake_kg

        lat, lon = get_effective_coordinates(request.user)
        weather = fetch_current_weather(lat, lon)
        if weather is not None:
            initial["temperature_c"] = weather["temperature_c"]
            initial["humidity_pct"] = weather["humidity_pct"]

        form = DailyLogForm(initial=initial, active_flock=active_flock)
        if weather is not None:
            # Only overridden when the fetch actually succeeded, so a failed/unconfigured
            # lookup leaves DailyLog's default model help_text untouched, same as before
            # this existed.
            form.fields["temperature_c"].help_text = (
                "Suggested from today's local weather — check against your own "
                "thermometer reading and adjust if needed."
            )
            form.fields["humidity_pct"].help_text = (
                "Suggested from today's local weather — check against your own "
                "hygrometer reading and adjust if needed."
            )

    context = {"active_nav": "log_daily_data", "form": form}
    return render(request, "farm/log_daily_data.html", context)


RECORD_RANGE_CHOICES = {
    "7": "Last 7 days",
    "30": "Last 30 days",
    "90": "Last 90 days",
    "all": "All time",
}
DEFAULT_RECORD_RANGE = "30"


@login_required
def farm_records(request):
    """List DailyLog entries for one of the owner's flocks, filtered by date range,
    flock (generation), and caging period.

    Defaults to the active flock so the common case is unchanged; if the owner has no
    active flock (e.g. every generation so far has been retired), falls back to their
    most recent flock by generation_number rather than showing an empty page. The
    caging-period dropdown's options are always derived from the *currently selected*
    flock — caging_period is a running counter over the owner's whole timeline (see
    services.assign_caging_periods), so a period number valid for one flock generation
    is meaningless for another. An invalid/stale ?period= (e.g. left over after
    switching flocks) silently falls back to "all", the same way an invalid ?range=
    already falls back to DEFAULT_RECORD_RANGE.
    """

    owner_flocks = list(Flock.objects.filter(owner=request.user).order_by("-generation_number"))
    flock_choices = {
        str(f.id): f"Generation {f.generation_number}" + (" (active)" if f.is_active else " (retired)")
        for f in owner_flocks
    }
    flocks_by_id = {str(f.id): f for f in owner_flocks}

    active_flock = get_active_flock(request.user)
    default_flock = active_flock or (owner_flocks[0] if owner_flocks else None)
    selected_flock = flocks_by_id.get(request.GET.get("flock"), default_flock)
    selected_flock_id = str(selected_flock.id) if selected_flock else ""

    selected_range = request.GET.get("range", DEFAULT_RECORD_RANGE)
    if selected_range not in RECORD_RANGE_CHOICES:
        selected_range = DEFAULT_RECORD_RANGE

    logs = (
        DailyLog.objects.filter(flock=selected_flock).order_by("-date")
        if selected_flock else DailyLog.objects.none()
    )

    if selected_range != "all":
        cutoff = timezone.localdate() - timedelta(days=int(selected_range))
        logs = logs.filter(date__gte=cutoff)

    period_values = (
        sorted(DailyLog.objects.filter(flock=selected_flock).values_list("caging_period", flat=True).distinct())
        if selected_flock else []
    )
    period_choices = {"all": "All periods", **{str(p): f"Period {p}" for p in period_values}}

    selected_period = request.GET.get("period", "all")
    if selected_period not in period_choices:
        selected_period = "all"
    if selected_period != "all":
        logs = logs.filter(caging_period=int(selected_period))

    context = {
        "active_nav": "records",
        "logs": logs,
        "range_choices": RECORD_RANGE_CHOICES,
        "selected_range": selected_range,
        "flock_choices": flock_choices,
        "selected_flock_id": selected_flock_id,
        "period_choices": period_choices,
        "selected_period": selected_period,
    }
    return render(request, "farm/farm_records.html", context)


@login_required
def farm_record_edit(request, pk):
    """Edit an existing DailyLog, recording a DailyLogEdit audit row per changed field.

    CLAUDE.md requires historical data edits to be tracked, never silently
    overwritten — this compares the submitted form against the record's current
    values field by field before saving, so nothing changes without a paired audit
    entry (old value, new value, who, when).
    """

    daily_log = get_object_or_404(DailyLog, pk=pk, flock__owner=request.user)
    if daily_log.is_locked:
        messages.error(
            request,
            "This record was used to train a forecasting model and can no longer be edited or deleted.",
        )
        return redirect("farm_records")
    # Snapshot old values before the form touches the instance: ModelForm.is_valid()
    # calls _post_clean(), which writes cleaned data onto form.instance (the same
    # object as daily_log) even before .save() — so reading daily_log's fields after
    # is_valid() would already see the new values, not the old ones.
    old_values = {field_name: getattr(daily_log, field_name) for field_name in AUDITED_FIELDS}

    if request.method == "POST":
        form = DailyLogEditForm(request.POST, instance=daily_log)
        if form.is_valid():
            changes = []
            for field_name in AUDITED_FIELDS:
                old_value = old_values[field_name]
                new_value = form.cleaned_data[field_name]
                if old_value != new_value:
                    changes.append((field_name, old_value, new_value))

            updated_log = form.save()
            for field_name, old_value, new_value in changes:
                DailyLogEdit.objects.create(
                    daily_log=updated_log,
                    field_name=field_name,
                    old_value=str(old_value),
                    new_value=str(new_value),
                    changed_by=request.user,
                )
            messages.success(request, f"Record updated ({len(changes)} field(s) changed)." if changes else "No changes made.")
            return redirect("farm_records")
    else:
        form = DailyLogEditForm(instance=daily_log)

    context = {"active_nav": "records", "form": form, "daily_log": daily_log}
    return render(request, "farm/farm_record_edit.html", context)


@login_required
def farm_record_delete(request, pk):
    """Permanently remove a DailyLog entry, after a confirmation step.

    Deletion is irreversible (unlike farm_record_edit, there's no audit row to
    reconstruct the record from), so this is deliberately a separate confirm page
    rather than a button on the edit form that deletes on click. Only the DailyLog
    itself and its DailyLogEdit history (on_delete=CASCADE) are removed.

    generate_forecast always writes its Forecast at forecast_date == the source
    DailyLog's own date (it's a same-day nowcast, see services.py), so that Forecast
    row is this log's alone, not shared with any other log — it's deleted alongside
    the DailyLog (its Recommendations cascade with it) so the dashboard's "latest
    forecast"/recommendations can't keep showing predictions for a date that no
    longer has any underlying farm data. Other Forecasts that merely used this log
    as a lag1/roll3 prior (via source_logs M2M) are left intact — their own
    forecast_date still has a real DailyLog behind it, just with one fewer historical
    input than when they were generated.
    """

    daily_log = get_object_or_404(DailyLog, pk=pk, flock__owner=request.user)
    if daily_log.is_locked:
        messages.error(
            request,
            "This record was used to train a forecasting model and can no longer be edited or deleted.",
        )
        return redirect("farm_records")

    if request.method == "POST":
        log_date = daily_log.date
        flock = daily_log.flock
        daily_log.delete()
        Forecast.objects.filter(flock=flock, forecast_date=log_date).delete()
        messages.success(request, f"Record for {log_date} deleted.")
        return redirect("farm_records")

    context = {"active_nav": "records", "daily_log": daily_log}
    return render(request, "farm/farm_record_delete_confirm.html", context)


@login_required
def flock_profile(request):
    """View the active flock's lifecycle info, or start the farm's very first flock
    if none exists yet.

    This is the only farmer-facing way to manage Flock rows at all — previously
    Flock could only be created/edited via the Django admin. started_on is always set
    to today's date by this view (not farmer-entered) and is not farmer-editable
    afterwards, so there is nothing here that needs to go through the DailyLogEdit
    audit trail — that requirement (CLAUDE.md, itikcare-spec.md section 3) covers
    historical DailyLog data, not Flock lifecycle metadata.

    A GET request with ?partial=1 renders just the profile card, no header/sidebar —
    this is what the header avatar's floating modal (base.html) fetches so it can show
    Flock Profile over whatever page the farmer is currently on. The plain /flock/
    page (no query param) still renders normally for direct links/bookmarks or
    browsers without JS, sharing the same "farm/_flock_profile_panel.html" partial.
    """

    template_name = (
        "farm/_flock_profile_panel.html" if request.GET.get("partial") == "1" else "farm/flock_profile.html"
    )
    active_flock = get_active_flock(request.user)

    if active_flock is None:
        if request.method == "POST":
            form = FlockRegisterForm(request.POST)
            if form.is_valid():
                # Max(), not a simple count, since a retired flock's generation_number
                # must never be reused (unique_generation_per_owner) — this is 1 on a
                # farm's very first-ever registration and old_flock.generation_number + 1
                # after any later retirement.
                last_generation = Flock.objects.filter(owner=request.user).aggregate(
                    Max("generation_number")
                )["generation_number__max"] or 0
                Flock.objects.create(
                    owner=request.user,
                    generation_number=last_generation + 1,
                    started_on=date.today(),
                    pending_flock_size=form.cleaned_data["flock_size"],
                    pending_flock_age_weeks=form.cleaned_data["flock_age_weeks"],
                    pending_feed_intake_kg=form.cleaned_data["feed_intake_kg"],
                )
                messages.success(request, "Flock registered.")
                return redirect("flock_profile")
        else:
            form = FlockRegisterForm()
        context = {"active_nav": "flock_profile", "active_flock": None, "form": form}
        return render(request, template_name, context)

    latest_log = DailyLog.objects.filter(flock=active_flock).order_by("-date").first()
    resume_form = None
    if not active_flock.is_caged:
        resume_initial = {"flock_size": latest_log.flock_size} if latest_log else {}
        resume_form = FlockResumeCagingForm(initial=resume_initial)

    context = {
        "active_nav": "flock_profile",
        "active_flock": active_flock,
        "latest_log": latest_log,
        # Calendar-projected, not the raw snapshot on latest_log — ducks keep aging
        # even during a logging gap (e.g. free-range), so "Average Age" must reflect
        # today's date, not whatever date latest_log happens to be from. Falls back to
        # pending_flock_age_weeks (confirmed at registration/resume-caging) when no
        # DailyLog exists yet at all, so the profile shows real numbers right away
        # instead of "—" until the first entry is logged.
        "current_age_weeks": current_flock_age_weeks(latest_log) or active_flock.pending_flock_age_weeks,
        "resume_form": resume_form,
    }
    return render(request, template_name, context)


@login_required
@require_POST
def flock_retire(request):
    """Retire the active flock. No replacement flock is created here — the farmer
    registers the next generation from scratch via the same Register Flock form
    flock_profile already shows when a farm has no active flock at all (this keeps
    first-ever registration and post-retirement registration identical).
    """

    active_flock = get_active_flock(request.user)
    if active_flock is None:
        messages.error(request, "No active flock to retire.")
        return redirect("flock_profile")

    active_flock.is_active = False
    active_flock.save(update_fields=["is_active"])
    # Retirement closes out this generation's entire history at once — a
    # complete new segment of training data now exists.
    trigger_retrain("flock_retired", active_flock.owner_id)
    messages.success(request, "Flock retired. Register a new flock when you're ready to start the next generation.")
    messages.info(request, "Model retraining has been triggered in the background.")
    return redirect("flock_profile")


@login_required
@require_POST
def toggle_caging_status(request):
    """Flip the active flock between caged (logging active) and free-range in the field.

    Nothing is deleted when going free-range — Dashboard/Forecast views just stop
    querying/displaying data while is_caged is False, and it all reappears once the
    flock is marked caged again.
    """

    active_flock = get_active_flock(request.user)
    if active_flock is None:
        messages.error(request, "No active flock to update.")
        return redirect("flock_profile")

    active_flock.is_caged = not active_flock.is_caged
    active_flock.save(update_fields=["is_caged"])
    if active_flock.is_caged:
        messages.success(request, "Flock marked as caged. Daily logging and forecasts have resumed.")
    else:
        messages.success(request, "Flock marked as free-range. It's out in the field — logging and forecasts are paused until it's caged again.")
    return redirect("flock_profile")


@login_required
@require_POST
def resume_caging(request):
    """Mark a free-range flock as caged again, capturing the farmer-confirmed duck
    count at the same time (ducks may have been added or lost while out in the field).

    The confirmed count is staged on Flock.pending_flock_size rather than written
    straight to a DailyLog — there's no daily record for "today" yet at this point,
    only a flock_size. It's picked up as the flock_size prefill on the farmer's next
    log_daily_data entry and cleared once that entry is actually saved.
    """

    active_flock = get_active_flock(request.user)
    if active_flock is None or active_flock.is_caged:
        messages.error(request, "No free-range flock to resume caging for.")
        return redirect("flock_profile")

    form = FlockResumeCagingForm(request.POST)
    if form.is_valid():
        active_flock.is_caged = True
        active_flock.pending_flock_size = form.cleaned_data["flock_size"]
        active_flock.save(update_fields=["is_caged", "pending_flock_size"])
        messages.success(
            request,
            f"Flock marked as caged with {form.cleaned_data['flock_size']} ducks. "
            "Daily logging and forecasts have resumed.",
        )
    else:
        messages.error(request, form.errors["flock_size"][0])
    return redirect("flock_profile")
