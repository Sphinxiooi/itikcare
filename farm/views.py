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

from .forms import DailyLogEditForm, DailyLogForm, FlockForm, FlockResumeCagingForm
from .models import DailyLog, DailyLogEdit, Flock
from .weather import fetch_current_weather

logger = logging.getLogger(__name__)

# Fields a DailyLog edit is audited against, and how to render each value as text
# for the DailyLogEdit.old_value/new_value CharFields.
AUDITED_FIELDS = ["date", "flock_size", "flock_age_weeks", "egg_count", "feed_intake_kg", "temperature_c", "humidity_pct"]

# A live entry more than this many days after the flock's previous log is treated as
# the start of a new caging period (i.e. the flock was free-ranged in between and has
# just been re-caged — itikcare-spec.md section 10). Chosen from the historical CSV
# import: every gap that stayed within one caging_period was <= 5 days, and every real
# caging-period boundary was >= 43 days, so 14 sits safely in between either reading.
CAGING_PERIOD_GAP_DAYS = 14


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
    """

    active_flock = Flock.objects.filter(is_active=True).order_by("-generation_number").first()
    if active_flock is None:
        messages.error(request, "No active flock exists yet. Create one from Flock Profile before logging daily data.")
        return redirect("dashboard")
    if not active_flock.is_caged:
        messages.error(request, "This flock is currently free-range in the field. Mark it as caged from Flock Profile before logging daily data.")
        return redirect("flock_profile")

    previous_log = DailyLog.objects.filter(flock=active_flock).order_by("-date").first()
    is_first_entry = previous_log is None

    if request.method == "POST":
        form = DailyLogForm(request.POST)
        if form.is_valid():
            new_date = form.cleaned_data["date"]
            if DailyLog.objects.filter(flock=active_flock, date=new_date).exists():
                form.add_error("date", "A record for this date already exists — edit it from Farm Records instead.")
            else:
                daily_log = form.save(commit=False)
                daily_log.flock = active_flock
                is_new_period = False
                if is_first_entry:
                    # caging_period is a globally unique segment marker (see
                    # forecasting/pipeline.py's SEGMENT_COLUMN grouping, which has no
                    # notion of `flock` at all) — a flock-retirement reset must continue
                    # the counter, not restart it at 1, or this flock's first rows would
                    # collide with an earlier flock's period 1 and get merged into the
                    # same training segment (itikcare-spec.md section 10).
                    max_caging_period = DailyLog.objects.aggregate(Max("caging_period"))["caging_period__max"]
                    daily_log.caging_period = (max_caging_period or 0) + 1
                else:
                    gap_days = (new_date - previous_log.date).days
                    is_new_period = gap_days > CAGING_PERIOD_GAP_DAYS
                    daily_log.caging_period = (
                        previous_log.caging_period + 1
                        if is_new_period
                        else previous_log.caging_period
                    )
                daily_log.recorded_by = request.user
                daily_log.save()
                if active_flock.pending_flock_size is not None:
                    # Consumed only now that it's actually backed a real DailyLog, so an
                    # abandoned form (farmer navigates away without logging) doesn't
                    # silently lose the count they confirmed at resume time.
                    active_flock.pending_flock_size = None
                    active_flock.save(update_fields=["pending_flock_size"])
                messages.success(request, "Daily data saved.")
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
                if is_new_period:
                    # The previous caging period just closed with this entry's gap — a
                    # complete new segment of training data now exists (itikcare-spec.md
                    # section 5's "rolling retraining as new data comes in").
                    trigger_retrain("caging_period_closed")
                    messages.info(request, "A new caging period was detected; model retraining has been triggered in the background.")
                return redirect("dashboard")
    else:
        initial = {}
        if previous_log is not None:
            weeks_since_last_log = (date.today() - previous_log.date).days // 7
            initial["flock_age_weeks"] = previous_log.flock_age_weeks + weeks_since_last_log
            initial["flock_size"] = previous_log.flock_size
        if active_flock.pending_flock_size is not None:
            # Overrides previous_log.flock_size (or fills it in on a first-ever entry)
            # with the count the farmer confirmed when resuming caging, since that's
            # more current than whatever was last logged before the free-range gap.
            initial["flock_size"] = active_flock.pending_flock_size

        weather = fetch_current_weather()
        if weather is not None:
            initial["temperature_c"] = weather["temperature_c"]
            initial["humidity_pct"] = weather["humidity_pct"]

        form = DailyLogForm(initial=initial)
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
    """List recent DailyLog entries for the active flock, filtered by a date range (default: last 30 days)."""

    selected_range = request.GET.get("range", DEFAULT_RECORD_RANGE)
    if selected_range not in RECORD_RANGE_CHOICES:
        selected_range = DEFAULT_RECORD_RANGE

    active_flock = Flock.objects.filter(is_active=True).order_by("-generation_number").first()
    logs = DailyLog.objects.filter(flock=active_flock).order_by("-date") if active_flock else DailyLog.objects.none()

    if selected_range != "all":
        cutoff = timezone.localdate() - timedelta(days=int(selected_range))
        logs = logs.filter(date__gte=cutoff)

    context = {
        "active_nav": "records",
        "logs": logs,
        "range_choices": RECORD_RANGE_CHOICES,
        "selected_range": selected_range,
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

    daily_log = get_object_or_404(DailyLog, pk=pk)
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

    daily_log = get_object_or_404(DailyLog, pk=pk)

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
    """View the active flock's lifecycle info, fix its start date, or start the
    farm's very first flock if none exists yet.

    This is the only farmer-facing way to manage Flock rows at all — previously
    Flock could only be created/edited via the Django admin. Editing started_on here
    is deliberately not run through the DailyLogEdit audit trail: that requirement
    (CLAUDE.md, itikcare-spec.md section 3) covers historical DailyLog data, not
    Flock lifecycle metadata.

    A GET request with ?partial=1 renders just the profile card, no header/sidebar —
    this is what the header avatar's floating modal (base.html) fetches so it can show
    Flock Profile over whatever page the farmer is currently on. The plain /flock/
    page (no query param) still renders normally for direct links/bookmarks or
    browsers without JS, sharing the same "farm/_flock_profile_panel.html" partial.
    """

    template_name = (
        "farm/_flock_profile_panel.html" if request.GET.get("partial") == "1" else "farm/flock_profile.html"
    )
    active_flock = Flock.objects.filter(is_active=True).order_by("-generation_number").first()

    if active_flock is None:
        if request.method == "POST":
            form = FlockForm(request.POST)
            if form.is_valid():
                flock = form.save(commit=False)
                flock.generation_number = 1
                flock.save()
                messages.success(request, "Flock created.")
                return redirect("flock_profile")
        else:
            form = FlockForm()
        context = {"active_nav": "flock_profile", "active_flock": None, "form": form}
        return render(request, template_name, context)

    if request.method == "POST":
        form = FlockForm(request.POST, instance=active_flock)
        if form.is_valid():
            form.save()
            messages.success(request, "Flock start date updated.")
            return redirect("flock_profile")
    else:
        form = FlockForm(instance=active_flock)

    latest_log = DailyLog.objects.filter(flock=active_flock).order_by("-date").first()
    resume_form = None
    if not active_flock.is_caged:
        resume_initial = {"flock_size": latest_log.flock_size} if latest_log else {}
        resume_form = FlockResumeCagingForm(initial=resume_initial)

    context = {
        "active_nav": "flock_profile",
        "active_flock": active_flock,
        "latest_log": latest_log,
        "form": form,
        "resume_form": resume_form,
    }
    return render(request, template_name, context)


@login_required
def flock_retire(request):
    """Retire the active flock and start a new generation.

    No extra bookkeeping is needed for the new flock's first daily entry: log_daily_
    data's is_first_entry check already keys off whether any DailyLog exists for the
    active flock, and the new Flock row starts with none.
    """

    active_flock = Flock.objects.filter(is_active=True).order_by("-generation_number").first()
    if active_flock is None:
        messages.error(request, "No active flock to retire.")
        return redirect("flock_profile")

    if request.method == "POST":
        form = FlockForm(request.POST)
        if form.is_valid():
            active_flock.is_active = False
            active_flock.save(update_fields=["is_active"])
            Flock.objects.create(
                generation_number=active_flock.generation_number + 1,
                started_on=form.cleaned_data["started_on"],
                is_active=True,
            )
            # Retirement closes out this generation's entire history at once — a
            # complete new segment of training data now exists.
            trigger_retrain("flock_retired")
            messages.success(request, "Flock retired. A new generation has been started.")
            messages.info(request, "Model retraining has been triggered in the background.")
            return redirect("flock_profile")
    else:
        form = FlockForm()

    context = {"active_nav": "flock_profile", "active_flock": active_flock, "form": form}
    return render(request, "farm/flock_retire_confirm.html", context)


@login_required
@require_POST
def toggle_caging_status(request):
    """Flip the active flock between caged (logging active) and free-range in the field.

    Nothing is deleted when going free-range — Dashboard/Forecast views just stop
    querying/displaying data while is_caged is False, and it all reappears once the
    flock is marked caged again.
    """

    active_flock = Flock.objects.filter(is_active=True).order_by("-generation_number").first()
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

    active_flock = Flock.objects.filter(is_active=True).order_by("-generation_number").first()
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
