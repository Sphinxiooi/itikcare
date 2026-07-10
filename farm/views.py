import logging
from datetime import date

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.shortcuts import get_object_or_404, redirect, render

from forecasting.models import Forecast
from forecasting.services import ModelNotTrainedError, generate_forecast, trigger_retrain

from .forms import DailyLogEditForm, DailyLogForm, FlockForm
from .models import DailyLog, DailyLogEdit, Flock

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
    """

    active_flock = Flock.objects.filter(is_active=True).order_by("-generation_number").first()
    if active_flock is None:
        messages.error(request, "No active flock exists yet. Create one from Flock Profile before logging daily data.")
        return redirect("dashboard")

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
                    daily_log.caging_period = 1
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
        form = DailyLogForm(initial=initial)

    context = {"active_nav": "log_daily_data", "form": form}
    return render(request, "farm/log_daily_data.html", context)


@login_required
def farm_records(request):
    """List recent DailyLog entries for the active flock."""

    active_flock = Flock.objects.filter(is_active=True).order_by("-generation_number").first()
    logs = DailyLog.objects.filter(flock=active_flock).order_by("-date") if active_flock else DailyLog.objects.none()

    context = {"active_nav": "records", "logs": logs}
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
    """

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
        return render(request, "farm/flock_profile.html", context)

    if request.method == "POST":
        form = FlockForm(request.POST, instance=active_flock)
        if form.is_valid():
            form.save()
            messages.success(request, "Flock start date updated.")
            return redirect("flock_profile")
    else:
        form = FlockForm(instance=active_flock)

    latest_log = DailyLog.objects.filter(flock=active_flock).order_by("-date").first()
    context = {
        "active_nav": "flock_profile",
        "active_flock": active_flock,
        "latest_log": latest_log,
        "form": form,
    }
    return render(request, "farm/flock_profile.html", context)


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
