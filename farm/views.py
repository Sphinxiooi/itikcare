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
from forecasting.services import ModelNotTrainedError, generate_forecast, readiness_for_log, trigger_retrain

from .forms import DailyLogEditForm, DailyLogForm, FlockRegisterForm, FlockResumeCagingForm
from .models import AUDITED_FIELDS, DailyLog, DailyLogEdit, Flock
from .services import (
    RECORDS_CHART_MIN_LOGS,
    assign_caging_periods,
    build_records_chart_data,
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

    temperature_c and humidity_pct are always left blank for the farmer to enter
    from their own thermometer/hygrometer reading — no pre-fill.

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
                # for this rule, shared with the import_daily_logs command — a flock's very
                # first entry always starts a new period (no prior date to gap-check
                # against, and it continues this owner's overall counter across flock
                # retirements per itikcare-spec.md section 10); later entries compare
                # against the previous log's date.
                daily_log.caging_period = assign_caging_periods(active_flock, request.user, [new_date])[0]
                is_new_period = not is_first_entry and daily_log.caging_period != previous_log.caging_period
                daily_log.recorded_by = request.user
                try:
                    # The .exists() check above is a plain read with nothing locked in
                    # between it and this save — a second, near-simultaneous submission
                    # for the same date (a double-tap on a slow connection, or the same
                    # form open in two tabs) can pass that same check before either has
                    # committed. DailyLog.Meta's UniqueConstraint is the real guard; a
                    # savepoint here (transaction.atomic, nested — see Django's docs on
                    # atomic blocks) means only this insert is rolled back on a
                    # collision, so the request can still render a normal response
                    # instead of leaving the outer request transaction unusable.
                    with transaction.atomic():
                        daily_log.save()
                except IntegrityError:
                    form.add_error("date", "A record for this date already exists — edit it from Farm Records instead.")
                else:
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
                    messages.success(request, _("Daily data saved."))
                    if new_date == operational_today():
                        try:
                            forecast = generate_forecast(daily_log)
                            if not forecast.has_prediction:
                                # Warm-up: recommendations were generated, the yield
                                # prediction was withheld (see forecast_readiness).
                                warmup = readiness_for_log(daily_log)
                                messages.info(
                                    request,
                                    _(
                                        "%(so_far)s/%(needed)s days logged. Egg yield "
                                        "forecasts start once there's enough of your own data. "
                                        "Recommendations are ready now."
                                    ) % {"so_far": warmup.logs_so_far, "needed": warmup.logs_needed},
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
                        # Backfilling a missed past day: still saved as ordinary history (see
                        # the log_daily_data docstring), just never gets its own forecast.
                        messages.info(
                            request,
                            _("This entry backfills a past date, so it won't generate its own "
                            "forecast — it's still saved as history and will feed into future "
                            "predictions."),
                        )
                    if is_new_period:
                        # The previous caging period just closed with this entry's gap — a
                        # complete new segment of training data now exists (itikcare-spec.md
                        # section 5's "rolling retraining as new data comes in").
                        trigger_retrain("caging_period_closed", active_flock.owner_id)
                        messages.info(request, _("New caging period detected — model retraining triggered."))
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

        form = DailyLogForm(initial=initial, active_flock=active_flock)

    context = {"active_nav": "log_daily_data", "form": form}
    return render(request, "farm/log_daily_data.html", context)


@login_required
@require_GET
def weather_for_date(request):
    """JSON endpoint backing log_daily_data.html's date-change handler: suggests
    temperature_c/humidity_pct for a backdated entry from historical weather (see
    farm.weather.fetch_historical_weather's docstring for the archive/forecast
    fallback it uses).

    Takes ?date=YYYY-MM-DD. Returns {"temperature_c": .., "humidity_pct": ..} as JSON,
    or JSON null if the date is missing/malformed, today or in the future (matching
    log_daily_data's own "today stays blank, no pre-fill" rule — see its docstring),
    or the lookup otherwise came back empty. Never a 4xx/5xx for a bad date: the
    frontend only acts on a non-null body, so there's nothing gained by distinguishing
    "bad input" from "no weather data available" at the HTTP-status level here.
    """
    try:
        target_date = date.fromisoformat(request.GET.get("date", ""))
    except ValueError:
        return JsonResponse(None, safe=False)

    latitude, longitude = get_effective_coordinates(request.user)
    weather = fetch_historical_weather(target_date, latitude, longitude)
    return JsonResponse(weather, safe=False)


@login_required
def farm_records(request):
    """List DailyLog entries for one of the owner's flocks, filtered by flock number
    and month.

    Defaults to the active flock so the common case is unchanged; if the owner has no
    active flock (e.g. every flock so far has been retired), falls back to their most
    recent flock by generation_number rather than showing an empty page. Retired
    flocks stay selectable (and their records visible) but are read-only — see
    farm_record_edit's is_active guard. The month dropdown's options are always
    derived from the *currently selected* flock's own records, so switching flocks
    resets a now-meaningless ?month= back to "all".
    """

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

    logs = (
        DailyLog.objects.filter(flock=selected_flock).order_by("-date").prefetch_related("edits")
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

    # Evaluated once here (rather than left as a queryset) since both the chart/
    # summary builders and the min-logs guard below need to inspect it more than
    # once -- a queryset would otherwise re-hit the database for each access.
    logs = list(logs)

    # A month-over-month delta on the stat tiles only makes sense when a specific
    # month is selected -- "All months" has no natural "previous period" to diff
    # against, so previous_month_logs stays [] and build_records_summary returns
    # every delta as None in that case.
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
    }
    if context["records_charts_available"]:
        context.update(build_records_chart_data(logs))
        context.update(build_records_summary(logs, previous_month_logs))
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
            _("This record was used to train a forecasting model and can no longer be edited or deleted."),
        )
        return redirect("farm_records")
    if not daily_log.flock.is_active:
        messages.error(
            request,
            _("This record belongs to a retired flock and is read-only."),
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
            # DailyLogEditForm can't enforce the unique (flock, date) constraint itself
            # — `flock` isn't one of its fields, and Django skips a constraint whose
            # fields are all excluded from validation — so a collision would otherwise
            # only surface as an IntegrityError from form.save(). Check it here, the
            # same way log_daily_data does on create.
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
                # Same race as log_daily_data's own create path: the .exists() check
                # above is a plain read, so a second, near-simultaneous edit/create for
                # the same date can still slip past it before either has committed.
                # One savepoint around the save + its audit rows also means a farmer
                # never ends up with a DailyLogEdit trail for an update that didn't
                # actually take (see this view's docstring on the audit trail).
                with transaction.atomic():
                    updated_log = form.save()
                    if updated_log.date != old_values["date"]:
                        # The date moved — its caging_period (assigned from the gap to the
                        # previous log when this row was first created) may no longer fit.
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
                messages.success(
                    request,
                    _("Record updated (%(count)d field(s) changed).") % {"count": len(changes)}
                    if changes
                    else _("No changes made."),
                )
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


def flock_profile_context(user):
    """The active-flock context shared by flock_profile below and accounts.views.
    account_settings (which embeds the same "farm/_flock_profile_panel.html" partial
    as its flock-status box) -- kept in one place so both pages show identical numbers.
    """
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
        # Calendar-projected, not the raw snapshot on latest_log — ducks keep aging
        # even during a logging gap (e.g. free-range), so "Average Age" must reflect
        # today's date, not whatever date latest_log happens to be from. Falls back to
        # pending_flock_age_weeks (confirmed at registration/resume-caging) when no
        # DailyLog exists yet at all, so the profile shows real numbers right away
        # instead of "—" until the first entry is logged.
        "current_age_weeks": current_flock_age_weeks(latest_log) or active_flock.pending_flock_age_weeks,
        "resume_form": resume_form,
    }


@login_required
def flock_profile(request):
    """View the active flock's lifecycle info, or start the farm's very first flock
    if none exists yet.

    This is the only farmer-facing way to manage Flock rows at all — previously
    Flock could only be created/edited via the Django admin. started_on is farmer-
    entered at registration (defaults to today, but can be backdated for a flock
    that's been raised a while before the farmer started using this app) and is not
    farmer-editable afterwards, so there is nothing here that needs to go through the
    DailyLogEdit audit trail — that requirement (CLAUDE.md, itikcare-spec.md section 3)
    covers historical DailyLog data, not Flock lifecycle metadata.

    A GET request with ?partial=1 renders just the profile card, no header/sidebar —
    available for embedding elsewhere, same as accounts.views.account_settings does
    with "farm/_flock_profile_panel.html" as its own second box. The plain /flock/
    page (no query param) renders normally for direct links/bookmarks.
    """

    template_name = (
        "farm/_flock_profile_panel.html" if request.GET.get("partial") == "1" else "farm/flock_profile.html"
    )
    active_flock = get_active_flock(request.user)

    if active_flock is None and request.method == "POST":
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
                started_on=form.cleaned_data["started_on"],
                pending_flock_size=form.cleaned_data["flock_size"],
                pending_flock_age_weeks=form.cleaned_data["flock_age_weeks"],
                pending_feed_intake_kg=form.cleaned_data["feed_intake_kg"],
            )
            messages.success(request, _("Flock registered."))
            return redirect("flock_profile")
        context = {"active_nav": "flock_profile", "active_flock": None, "form": form}
        return render(request, template_name, context)

    context = {"active_nav": "flock_profile", **flock_profile_context(request.user)}
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
        messages.error(request, _("No active flock to retire."))
        return redirect("flock_profile")

    active_flock.is_active = False
    active_flock.save(update_fields=["is_active"])
    # Retirement closes out this generation's entire history at once — a
    # complete new segment of training data now exists.
    trigger_retrain("flock_retired", active_flock.owner_id)
    messages.success(request, _("Flock retired. Register a new flock when you're ready to start the next generation."))
    messages.info(request, _("Model retraining has been triggered in the background."))
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
        messages.error(request, _("No active flock to update."))
        return redirect("flock_profile")

    active_flock.is_caged = not active_flock.is_caged
    active_flock.save(update_fields=["is_caged"])
    if active_flock.is_caged:
        messages.success(request, _("Flock marked as caged. Daily logging and forecasts have resumed."))
    else:
        messages.success(request, _("Flock marked as free-range. It's out in the field — logging and forecasts are paused until it's caged again."))
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
        messages.error(request, _("No free-range flock to resume caging for."))
        return redirect("flock_profile")

    form = FlockResumeCagingForm(request.POST)
    if form.is_valid():
        active_flock.is_caged = True
        active_flock.pending_flock_size = form.cleaned_data["flock_size"]
        active_flock.save(update_fields=["is_caged", "pending_flock_size"])
        messages.success(
            request,
            _("Flock marked as caged with %(size)s ducks. Daily logging and forecasts have resumed.")
            % {"size": form.cleaned_data["flock_size"]},
        )
    else:
        messages.error(request, form.errors["flock_size"][0])
    return redirect("flock_profile")
