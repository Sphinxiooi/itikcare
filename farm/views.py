from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.shortcuts import get_object_or_404, redirect, render

from .forms import DailyLogEditForm, DailyLogForm
from .models import DailyLog, DailyLogEdit, Flock

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

    flock_size isn't collected on this form for repeat entries (see
    forms.DailyLogForm docstring) — it's carried forward from the active flock's most
    recent DailyLog. caging_period is likewise never farmer-entered: it continues the
    previous log's value, unless the gap since that log is long enough to imply a
    free-range-then-recage cycle happened in between (CAGING_PERIOD_GAP_DAYS).
    """

    active_flock = Flock.objects.filter(is_active=True).order_by("-generation_number").first()
    if active_flock is None:
        messages.error(request, "No active flock exists yet. Create one in the admin before logging daily data.")
        return redirect("dashboard")

    previous_log = DailyLog.objects.filter(flock=active_flock).order_by("-date").first()
    is_first_entry = previous_log is None

    if request.method == "POST":
        form = DailyLogForm(request.POST, require_flock_size=is_first_entry)
        if form.is_valid():
            new_date = form.cleaned_data["date"]
            if DailyLog.objects.filter(flock=active_flock, date=new_date).exists():
                form.add_error("date", "A record for this date already exists — edit it from Farm Records instead.")
            else:
                daily_log = form.save(commit=False)
                daily_log.flock = active_flock
                if is_first_entry:
                    daily_log.flock_size = form.cleaned_data["flock_size"]
                    daily_log.caging_period = 1
                else:
                    daily_log.flock_size = previous_log.flock_size
                    gap_days = (new_date - previous_log.date).days
                    daily_log.caging_period = (
                        previous_log.caging_period + 1
                        if gap_days > CAGING_PERIOD_GAP_DAYS
                        else previous_log.caging_period
                    )
                daily_log.recorded_by = request.user
                daily_log.save()
                messages.success(request, "Daily data saved.")
                return redirect("dashboard")
    else:
        form = DailyLogForm(
            initial={"flock_age_weeks": previous_log.flock_age_weeks if previous_log else None},
            require_flock_size=is_first_entry,
        )

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
