"""Small ORM-facing helpers shared across farm/dashboard/forecasting views.

Kept here (rather than duplicated per-view) because "the active flock" is looked up
identically in many places, and every one of those lookups must be scoped to the
requesting farmer now that more than one farm's data lives in the same tables.
"""

import json
import re
import statistics
from bisect import bisect_right
from datetime import date, timedelta

from django.db.models import Max
from django.utils import timezone
from django.utils.formats import date_format
from django.utils.translation import gettext as _
from django.utils.translation import gettext_lazy, ngettext

from .models import DailyLog, Flock

# Trend-chart range choices shared by the dashboard and the Forecast & Recommendations
# page (see build_trend_chart_data below) -- kept as one definition so the two pages'
# dropdowns can never drift out of sync with each other. trend_range_choices_for appends
# one option per real calendar month on top of these three.
TREND_RANGE_OPTIONS = (
    ("7", gettext_lazy("Last 7 days")),
    ("30", gettext_lazy("Last 1 month")),
    ("all", gettext_lazy("All data")),
)

# A specific-month trend_range value looks like "2026-09" -- distinguishes it from the
# day-count ("7"/"30") and "all" options above wherever the two need different handling
# (build_trend_chart_data's query, and skipping the future-forecast tail for a past month).
MONTH_TREND_RANGE_RE = re.compile(r"^\d{4}-\d{2}$")


def trend_range_choices_for(flock):
    """TREND_RANGE_OPTIONS plus one option per real calendar month of DailyLog data for
    `flock`, newest month first -- lets the trend chart be pinned to one specific month,
    same convention as farm_records' month dropdown (farm/views.py::farm_records). `flock`
    may be None (no active flock yet), in which case no month options exist.
    """
    if flock is None:
        return TREND_RANGE_OPTIONS
    month_values = DailyLog.objects.filter(flock=flock).dates("date", "month", order="DESC")
    month_options = tuple((d.strftime("%Y-%m"), date_format(d, "F Y")) for d in month_values)
    return TREND_RANGE_OPTIONS + month_options

# Below this many prior logs for a flock, its own mean/spread isn't trustworthy enough
# to judge a new entry against — a brand-new flock's first few days have no baseline.
ANOMALY_MIN_HISTORY = 5

# A new value is flagged if it's more than this fraction away from the flock's own
# historical average, OR more than this many standard deviations away — whichever
# threshold is looser, so a flock with naturally tight numbers (low stdev) still gets
# a sane minimum tolerance, and a flock with naturally noisy numbers (high stdev)
# isn't flagged on ordinary day-to-day swings.
ANOMALY_PCT_THRESHOLD = 0.4
ANOMALY_STDEV_MULTIPLIER = 2.5

# (DailyLog field, label for the warning text) pairs checked against this flock's own
# history. Deliberately mirrors the model's manually-entered fields, not derived ones.
ANOMALY_CHECK_FIELDS = [
    ("egg_count", gettext_lazy("Egg count")),
    ("feed_intake_kg", gettext_lazy("Feed intake")),
    ("flock_size", gettext_lazy("Flock size")),
    ("temperature_c", gettext_lazy("Temperature")),
    ("humidity_pct", gettext_lazy("Humidity")),
]


def get_active_flock(owner):
    """The requesting farmer's own active Flock, or None if they don't have one yet."""
    return Flock.objects.filter(owner=owner, is_active=True).order_by("-generation_number").first()


def get_effective_coordinates(owner):
    """(latitude, longitude) to use for owner's weather lookups: their own farm
    location if they've set one (accounts.User.latitude/longitude, captured at signup),
    else (None, None) so farm.weather's own settings.FARM_LATITUDE/FARM_LONGITUDE
    fallback applies (the foundation farmer's location) — covers every farmer who
    signed up without sharing a location, including all pre-existing users.
    """
    if owner.latitude is not None and owner.longitude is not None:
        return owner.latitude, owner.longitude
    return None, None


# Duck egg collection that finishes in the early morning (e.g. 2-5am) still belongs to
# the previous day's collection cycle, not a fresh one -- so the farm's logging day
# rolls over at this local hour instead of at midnight.
FARM_DAY_START_HOUR = 6


def operational_today(now=None):
    """The farm's current logging day, rolling over at FARM_DAY_START_HOUR local time
    instead of midnight. Single source of truth for every "is this today" comparison
    touching DailyLog/Forecast dates (form validation, forecast generation, the
    dashboard's "logged today" state, the reminder command) — see itikcare-spec.md
    section 10 for why a plain calendar day doesn't match how this farm actually
    operates.

    timezone.localtime() is TIME_ZONE/USE_TZ-aware by construction, so this is immune
    to the naive datetime.date.today() bug documented in itikcare/settings.py's
    TIME_ZONE comment (that bug used the server's OS clock instead of Asia/Manila).

    now: override for tests, so a specific instant can be pinned instead of the real
    clock (e.g. to exercise the 6am boundary itself either side).
    """
    now = now or timezone.localtime()
    if now.hour < FARM_DAY_START_HOUR:
        return now.date() - timedelta(days=1)
    return now.date()


def current_flock_age_weeks(daily_log):
    """Project a DailyLog's flock_age_weeks forward to today's calendar date.

    daily_log.flock_age_weeks is a snapshot as of daily_log.date, not a live value —
    it goes stale as soon as a day passes without a new log (e.g. a free-range gap,
    itikcare-spec.md section 10), so anywhere the UI displays "current" flock age must
    add the calendar weeks elapsed since that snapshot rather than showing it as-is.
    Mirrors the prefill math in views.log_daily_data. Returns None if daily_log is None.

    Uses timezone.localdate() rather than operational_today(): a rough weekly bucket
    like this doesn't need the 6am cutoff, just the naive-date fix.
    """
    if daily_log is None:
        return None
    weeks_elapsed = (timezone.localdate() - daily_log.date).days // 7
    return daily_log.flock_age_weeks + weeks_elapsed


def caging_period_for_new_log(active_flock, owner):
    """The caging_period number for the next DailyLog of active_flock.

    A new caging period starts only when the flock has actually been out free-range and
    come back (itikcare-spec.md section 10): resume_caging / toggle_caging_status set
    Flock.starts_new_caging_period, and log_daily_data clears it once this log is saved.
    A plain gap in logging while the flock stayed caged -- the farmer just didn't log
    for a while -- continues the same period. The ducks were caged and laying the whole
    time, so it's the same operational segment, only with unrecorded days; the forecast
    shows a "may be less accurate" caution for it instead (see
    forecasting.services.current_logging_gap_caution).

    A flock's very first log always starts a new period, continuing this *owner's*
    overall caging_period counter (Max across all their flocks) rather than restarting
    at 1 -- a flock-generation reset is a segment boundary too.
    """
    previous_log = DailyLog.objects.filter(flock=active_flock).order_by("-date").first()
    if previous_log is None:
        max_caging_period = DailyLog.objects.filter(flock__owner=owner).aggregate(
            Max("caging_period")
        )["caging_period__max"]
        return (max_caging_period or 0) + 1
    if active_flock.starts_new_caging_period:
        return previous_log.caging_period + 1
    return previous_log.caging_period


def recompute_caging_period(daily_log):
    """Re-derive one DailyLog's caging_period after an edit moved its date.

    caging_period is assigned once, when the row is first logged (caging_period_for_new_log
    above). farm_record_edit audits a date change, but the stored period can then be out
    of order with its new neighbours -- e.g. a date moved from after a free-range return
    back to before it.

    Periods only ever increase with date, so the log's period is clamped between the
    periods of the logs now immediately before and after it:
    * a small date correction inside a period changes nothing;
    * the first log of a period moved slightly earlier (still after the previous
      period's last log) keeps its own, newer period;
    * a log moved deep into another period takes that period.

    It deliberately does NOT cascade to other rows: a farmer can't move a date far
    without the change being obvious in Farm Records, and a bulk re-segmentation, if it
    were ever needed, belongs in a management command, not a request cycle.
    """
    other_logs = DailyLog.objects.filter(flock=daily_log.flock).exclude(pk=daily_log.pk)
    previous_log = other_logs.filter(date__lt=daily_log.date).order_by("-date").first()
    next_log = other_logs.filter(date__gt=daily_log.date).order_by("date").first()

    new_period = daily_log.caging_period
    if previous_log is not None:
        new_period = max(new_period, previous_log.caging_period)
    if next_log is not None:
        new_period = min(new_period, next_log.caging_period)
    if new_period != daily_log.caging_period:
        daily_log.caging_period = new_period
        daily_log.save(update_fields=["caging_period"])


def detect_daily_log_anomalies(active_flock, cleaned_data):
    """Flag newly entered values that look far outside this flock's own history.

    Compares each field in ANOMALY_CHECK_FIELDS against the mean and spread of that
    flock's prior DailyLogs — e.g. an egg count of 700 when this flock has always
    logged around 350 gets caught here before it's saved and used to retrain the
    model. This is a soft check (surfaced to the farmer for one last look via the
    log_daily_data confirmation screen, not a hard validation error) since real farm
    conditions do genuinely shift over time and a true value shouldn't be unsaveable.

    Returns a list of human-readable warning strings; empty if nothing looks unusual.
    """
    warnings = []

    # A duck lays at most one egg a day, so today's egg count can't sensibly beat
    # today's own flock size — checked against this entry's own submitted flock_size,
    # not flock history, so it still catches a typo on a brand-new flock's very first
    # entry (before ANOMALY_MIN_HISTORY worth of logs exist to compare against below).
    if cleaned_data["egg_count"] > cleaned_data["flock_size"]:
        warnings.append(
            _(
                "You entered %(eggs)s eggs, but only %(ducks)s ducks. That's more eggs "
                "than ducks, which isn't normally possible — please check for a typo."
            ) % {"eggs": cleaned_data["egg_count"], "ducks": cleaned_data["flock_size"]}
        )

    history = DailyLog.objects.filter(flock=active_flock)
    if history.count() < ANOMALY_MIN_HISTORY:
        return warnings

    for field_name, label in ANOMALY_CHECK_FIELDS:
        past_values = [float(v) for v in history.values_list(field_name, flat=True)]
        mean = statistics.mean(past_values)
        stdev = statistics.pstdev(past_values)
        threshold = max(mean * ANOMALY_PCT_THRESHOLD, stdev * ANOMALY_STDEV_MULTIPLIER)
        new_value = float(cleaned_data[field_name])
        if threshold > 0 and abs(new_value - mean) > threshold:
            warnings.append(
                _("%(label)s of %(value)s is unusual for this flock — your average so far is about %(mean)s.")
                % {"label": label, "value": cleaned_data[field_name], "mean": f"{mean:.1f}"}
            )
    return warnings


def resolve_trend_range(raw_value, choices=TREND_RANGE_OPTIONS):
    """Validate a ?trend_range= GET param against `choices` (TREND_RANGE_OPTIONS by
    default, or TREND_RANGE_OPTIONS plus a flock's own month options -- see
    trend_range_choices_for), defaulting to "7"."""
    return raw_value if raw_value in dict(choices) else "7"


# How many days past its own date one Forecast still predicts (its
# predicted_next_day1/2/3_yield -- see forecasting/services.py::_predict_next_days).
FORECAST_HORIZON_DAYS = 3

# Which Forecast field predicts the day `horizon` days after its forecast_date. Horizon 0
# is the same-day prediction made from that day's own DailyLog.
FORECAST_FIELD_BY_HORIZON = {
    0: "predicted_daily_yield",
    1: "predicted_next_day1_yield",
    2: "predicted_next_day2_yield",
    3: "predicted_next_day3_yield",
}


def predictions_by_date(flock, start, end):
    """The newest available prediction for every date in [start, end] that has one.

    Every logged day produces a Forecast covering 4 dates: its own date (horizon 0, the
    same-day prediction) and the next 3 days (horizons 1-3). So a date can be predicted by
    up to 4 different Forecasts. The rule used everywhere a prediction is displayed (the
    Next 3-Day Forecast panel and the Egg Yield Trend chart) is simply: **the newest
    Forecast wins**. A logged day shows its own same-day prediction; a day that isn't
    logged (yet) shows yesterday's day+1, else the day before's day+2, else day+3. This
    is what keeps the predicted line and the panel from disappearing when the farmer
    hasn't logged today yet, or skipped a day.

    Returns {date: {"value", "forecast_date", "horizon"}} -- forecast_date/horizon are
    kept so every displayed number can be traced back to the Forecast that produced it.
    Warm-up Forecasts (no prediction -- see forecasting.services.forecast_readiness) are
    skipped, so an older real prediction is used instead if one covers that date.
    """
    # Local import: see build_trend_chart_data below for why.
    from forecasting.models import Forecast

    forecasts = Forecast.objects.filter(
        flock=flock,
        forecast_date__range=(start - timedelta(days=FORECAST_HORIZON_DAYS), end),
        predicted_daily_yield__isnull=False,
    ).order_by("forecast_date")

    predictions = {}
    # Oldest first, so a newer Forecast's value for the same date overwrites an older one.
    for forecast in forecasts:
        for horizon, field in FORECAST_FIELD_BY_HORIZON.items():
            target_date = forecast.forecast_date + timedelta(days=horizon)
            value = getattr(forecast, field)
            if start <= target_date <= end and value is not None:
                predictions[target_date] = {
                    "value": value,
                    "forecast_date": forecast.forecast_date,
                    "horizon": horizon,
                }
    return predictions


def build_next_day_forecasts(flock, today):
    """The Next 3-Day Forecast panel: the newest prediction (see predictions_by_date) for
    each upcoming day that hasn't been logged yet, from today through today + 3.

    Before today is logged, that is yesterday's forecast: Today / Tomorrow / day after.
    Once today is logged, today's own forecast takes over: Tomorrow / +2 / +3. If the
    farmer skipped a day or two, fewer days are still covered (e.g. a forecast from 2
    days ago only reaches Today and Tomorrow), and once the newest forecast is more than
    3 days old there's nothing left to show -- [] is returned.

    These are 3 distinct day-by-day numbers, not the single predicted_tri_day_yield sum
    -- see forecasting/services.py's _predict_next_days for how they're derived.
    """
    if flock is None:
        return []
    end = today + timedelta(days=FORECAST_HORIZON_DAYS)
    predictions = predictions_by_date(flock, today, end)
    logged_dates = set(DailyLog.objects.filter(flock=flock, date__range=(today, end)).values_list("date", flat=True))
    return [
        {
            "date": day,
            "value": prediction["value"],
            "is_today": day == today,
            "is_tomorrow": day == today + timedelta(days=1),
            "forecast_date": prediction["forecast_date"],
            "horizon": prediction["horizon"],
        }
        for day, prediction in sorted(predictions.items())
        if day not in logged_dates
    ]


# How many days one Tri-day Yield Model prediction covers: its Forecast's own date plus
# the next 2 days (forecasting/pipeline.py::add_tri_day_target).
TRI_DAY_WINDOW_DAYS = 3


def build_tri_day_forecast(flock, today):
    """The Tri-day Yield Model's 3-day total for the Forecast & Recommendations page, or
    None when there's none to show.

    Uses the newest Forecast with a tri-day prediction whose window still reaches today.
    The tri-day model predicts the total for its forecast_date plus the next 2 days, so a
    forecast from up to 2 days ago still covers today. start/end are returned so the page
    labels the exact dates the total covers -- this window starts on the forecast date
    itself, so it isn't the same 3 dates as the Next 3-Day panel's cards, which come from
    the Daily Yield Model (forecasting/services.py::_predict_next_days).

    None while the flock has just been re-caged and hasn't logged yet
    (Flock.starts_new_caging_period): any recent Forecast then belongs to the previous
    caging period, and a projection must never cross that boundary (itikcare-spec.md
    section 10). After the first new log, the warm-up notice hides it instead.
    """
    # Local import: see build_trend_chart_data below for why.
    from forecasting.models import Forecast

    if flock is None or flock.starts_new_caging_period:
        return None
    forecast = (
        Forecast.objects.filter(
            flock=flock,
            forecast_date__range=(today - timedelta(days=TRI_DAY_WINDOW_DAYS - 1), today),
            predicted_tri_day_yield__isnull=False,
        )
        .order_by("-forecast_date")
        .first()
    )
    if forecast is None:
        return None
    return {
        "value": forecast.predicted_tri_day_yield,
        "start": forecast.forecast_date,
        "end": forecast.forecast_date + timedelta(days=TRI_DAY_WINDOW_DAYS - 1),
        "forecast_date": forecast.forecast_date,
    }


def _prediction_source_label(prediction):
    """Tooltip text naming which Forecast a trend-chart point came from (traceability)."""
    if prediction["horizon"] == 0:
        return _("Same-day prediction")
    return ngettext(
        "Forecast from %(date)s (%(days)s day ahead)",
        "Forecast from %(date)s (%(days)s days ahead)",
        prediction["horizon"],
    ) % {"date": date_format(prediction["forecast_date"], "M j"), "days": prediction["horizon"]}


def build_trend_chart_data(active_flock, flock_is_caged, trend_range):
    """Chart.js-ready trend data shared by the dashboard and the Forecast & Recommendations
    page: oldest-to-newest, "actual" (from DailyLog) and "predicted" (from Forecast) side by
    side.

    trend_range must already be validated (see resolve_trend_range). "all" has no calendar
    cutoff -- every logged day for the flock. "7"/"30" are calendar-day cutoffs, not a
    count of rows -- with the historical logging gaps documented in itikcare-spec.md
    section 10, slicing to the last N *rows* instead could silently span far more than N
    calendar days, making the label and the chart's actual date range misleading. A
    "YYYY-MM" value (see trend_range_choices_for/MONTH_TREND_RANGE_RE) instead pins the
    chart to one specific calendar month, same convention as farm_records' month filter.

    "Predicted" for each date is the newest prediction available for it (see
    predictions_by_date): a logged day's own same-day prediction, otherwise the most recent
    earlier forecast that covers it. So the predicted line doesn't break on a day the
    farmer skipped, and it keeps running up to 3 days past the last log -- including today
    before today has been logged. Each point's value updates in place when a newer
    prediction for that date arrives.

    The x-axis is every logged date in range plus every *unlogged* date that has a
    prediction, subject to one guard (itikcare-spec.md section 10): an unlogged date is only
    shown while the farm is still in the same caging period as the forecast that predicted
    it. So the last log before a free-range gap never trails 3 projected days into the gap.
    trend_actual is None on unlogged dates (a real missing log stays a visible break in the
    actual line), and trend_projected marks them so the template can dash the predicted
    line there -- a projection is never visually mistaken for a prediction tied to a real
    log. A specific month view is clipped to that month and to today at the latest, so it
    never grows a future tail.
    """
    today = operational_today()
    is_month_view = bool(MONTH_TREND_RANGE_RE.match(trend_range))
    flock_logs = DailyLog.objects.filter(flock=active_flock)

    if trend_range == "all":
        trend_logs = list(flock_logs.order_by("-date")) if flock_is_caged else []
        start = trend_logs[-1].date if trend_logs else None
        end = today + timedelta(days=FORECAST_HORIZON_DAYS)
    elif is_month_view:
        year, month = (int(part) for part in trend_range.split("-"))
        start = date(year, month, 1)
        next_month_start = (start + timedelta(days=32)).replace(day=1)
        end = min(next_month_start - timedelta(days=1), today)
        trend_logs = list(flock_logs.filter(date__range=(start, end)).order_by("-date")) if flock_is_caged else []
    else:
        start = today - timedelta(days=int(trend_range) - 1)
        end = today + timedelta(days=FORECAST_HORIZON_DAYS)
        trend_logs = list(flock_logs.filter(date__gte=start).order_by("-date")) if flock_is_caged else []

    actual_by_date = {log.date: float(log.egg_count) for log in trend_logs}

    predicted_by_date = {}
    if trend_logs:
        # Caging period of every log a shown prediction could depend on: the forecast's own
        # source log (up to FORECAST_HORIZON_DAYS before start) and the next log after an
        # unlogged date (which may fall after `end`, e.g. early next month in a month view).
        period_by_date = dict(
            flock_logs.filter(date__gte=start - timedelta(days=FORECAST_HORIZON_DAYS))
            .order_by("date")
            .values_list("date", "caging_period")
        )
        log_dates = sorted(period_by_date)

        def projection_is_shown(day, prediction):
            next_log_index = bisect_right(log_dates, day)
            if next_log_index < len(log_dates):
                # Logged again afterwards: show it only if that was still the same caging
                # period as the forecast's own log -- not a return from free-range.
                next_log_date = log_dates[next_log_index]
                return period_by_date[next_log_date] == period_by_date.get(prediction["forecast_date"])
            # Nothing logged since: this is the live tail, shown while the forecast is still
            # recent enough to cover today onward (same window as build_next_day_forecasts).
            return prediction["forecast_date"] >= today - timedelta(days=FORECAST_HORIZON_DAYS)

        predicted_by_date = {
            day: prediction
            for day, prediction in predictions_by_date(active_flock, start, end).items()
            if day in actual_by_date or projection_is_shown(day, prediction)
        }

    trend_dates = sorted(set(actual_by_date) | set(predicted_by_date))
    # (strftime's day-without-zero-padding directive isn't portable across platforms,
    # so the day number is appended manually instead of using "%-d"/"%#d".)
    trend_labels = [date_format(d, "M j") for d in trend_dates]
    trend_actual = [actual_by_date.get(d) for d in trend_dates]
    trend_predicted = [
        float(predicted_by_date[d]["value"]) if d in predicted_by_date else None for d in trend_dates
    ]
    # True where the date has no log yet, so its prediction is a projection from an earlier
    # day's forecast -- drawn dashed.
    trend_projected = [d not in actual_by_date for d in trend_dates]
    trend_predicted_source = [
        _prediction_source_label(predicted_by_date[d]) if d in predicted_by_date else None for d in trend_dates
    ]

    return {
        "trend_logs": trend_logs,
        "trend_labels_json": json.dumps(trend_labels),
        "trend_actual_json": json.dumps(trend_actual),
        "trend_predicted_json": json.dumps(trend_predicted),
        "trend_projected_json": json.dumps(trend_projected),
        "trend_predicted_source_json": json.dumps(trend_predicted_source),
        "trend_has_future_forecast": any(trend_projected),
    }


# Below this many logs in the selected Farm Records filter, a chart/summary would
# be drawn from too little data to mean anything (a single point has no trend, and
# a month-over-month delta needs something to compare against) -- the view falls
# back to the plain table only.
RECORDS_CHART_MIN_LOGS = 2


def _feed_per_bird_grams(log):
    """Grams of feed per bird for one DailyLog -- mirrors recommendations/rules.py's
    _feed_per_bird_grams formula exactly (feed_intake_kg / flock_size * 1000), kept
    as a separate copy here rather than imported since recommendations depends on
    farm (not the other way around) and this one-line formula isn't worth a new
    cross-app dependency to share.
    """
    return (float(log.feed_intake_kg) / log.flock_size) * 1000


def build_records_chart_data(logs):
    """Chart.js-ready arrays for the Farm Records page's two panels -- egg yield vs.
    feed-per-bird, and temperature vs. humidity -- oldest-to-newest across whatever
    `logs` the caller already filtered by flock/month (see farm/views.py::farm_records).
    Caller is expected to only invoke this once len(logs) >= RECORDS_CHART_MIN_LOGS.
    """
    ordered_logs = sorted(logs, key=lambda log: log.date)
    labels = [date_format(log.date, "M j") for log in ordered_logs]

    return {
        "records_chart_labels_json": json.dumps(labels),
        "records_chart_egg_yield_json": json.dumps([log.egg_count for log in ordered_logs]),
        "records_chart_feed_per_bird_json": json.dumps(
            [round(_feed_per_bird_grams(log), 1) for log in ordered_logs]
        ),
        "records_chart_temperature_json": json.dumps([float(log.temperature_c) for log in ordered_logs]),
        "records_chart_humidity_json": json.dumps([float(log.humidity_pct) for log in ordered_logs]),
    }


def build_records_summary(logs, previous_month_logs):
    """Farm Records stat-tile values: averages over `logs` (the current flock/month
    filter) for egg yield, feed per bird, temperature and humidity, plus the most
    recently logged flock size. Each average also gets a month-over-month delta
    (percent change) against `previous_month_logs` when that's non-empty -- the
    caller only passes a real previous-month queryset when a specific month is
    selected (see farm/views.py::farm_records); "All months" has no natural
    previous period to compare against, so it passes [] and every delta comes
    back None.

    Caller is expected to only invoke this once len(logs) >= RECORDS_CHART_MIN_LOGS.
    """
    ordered_logs = sorted(logs, key=lambda log: log.date)
    latest_log = ordered_logs[-1]

    avg_egg_yield = statistics.fmean(log.egg_count for log in ordered_logs)
    avg_feed_per_bird = statistics.fmean(_feed_per_bird_grams(log) for log in ordered_logs)
    avg_temperature = statistics.fmean(float(log.temperature_c) for log in ordered_logs)
    avg_humidity = statistics.fmean(float(log.humidity_pct) for log in ordered_logs)

    def _delta_pct(current_avg, previous_values):
        if not previous_values:
            return None
        previous_avg = statistics.fmean(previous_values)
        if previous_avg == 0:
            return None
        return round((current_avg - previous_avg) / previous_avg * 100, 1)

    return {
        "avg_egg_yield": round(avg_egg_yield, 1),
        "avg_feed_per_bird": round(avg_feed_per_bird, 1),
        "avg_temperature": round(avg_temperature, 1),
        "avg_humidity": round(avg_humidity, 1),
        "current_flock_size": latest_log.flock_size,
        "egg_yield_delta_pct": _delta_pct(avg_egg_yield, [log.egg_count for log in previous_month_logs]),
        "feed_per_bird_delta_pct": _delta_pct(
            avg_feed_per_bird, [_feed_per_bird_grams(log) for log in previous_month_logs]
        ),
        "temperature_delta_pct": _delta_pct(
            avg_temperature, [float(log.temperature_c) for log in previous_month_logs]
        ),
        "humidity_delta_pct": _delta_pct(avg_humidity, [float(log.humidity_pct) for log in previous_month_logs]),
    }
