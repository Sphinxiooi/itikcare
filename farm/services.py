"""Small ORM-facing helpers shared across farm/dashboard/forecasting views.

Kept here (rather than duplicated per-view) because "the active flock" is looked up
identically in many places, and every one of those lookups must be scoped to the
requesting farmer now that more than one farm's data lives in the same tables.
"""

import json
import statistics
from datetime import date, timedelta

from django.db.models import Max

from .models import DailyLog, Flock

# Trend-chart range choices shared by the dashboard and the Forecast & Recommendations
# page (see build_trend_chart_data below) -- kept as one definition so the two pages'
# dropdowns can never drift out of sync with each other.
TREND_RANGE_OPTIONS = (("7", "Last 7 days"), ("30", "Last 1 month"), ("all", "All data"))

# A live entry more than this many days after the flock's previous log is treated as
# the start of a new caging period (i.e. the flock was free-ranged in between and has
# just been re-caged — itikcare-spec.md section 10). Chosen from the historical CSV
# import: every gap that stayed within one caging_period was <= 5 days, and every real
# caging-period boundary was >= 43 days, so 14 sits safely in between either reading.
CAGING_PERIOD_GAP_DAYS = 14

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
    ("egg_count", "Egg count"),
    ("feed_intake_kg", "Feed intake"),
    ("flock_size", "Flock size"),
    ("temperature_c", "Temperature"),
    ("humidity_pct", "Humidity"),
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


def current_flock_age_weeks(daily_log):
    """Project a DailyLog's flock_age_weeks forward to today's calendar date.

    daily_log.flock_age_weeks is a snapshot as of daily_log.date, not a live value —
    it goes stale as soon as a day passes without a new log (e.g. a free-range gap,
    itikcare-spec.md section 10), so anywhere the UI displays "current" flock age must
    add the calendar weeks elapsed since that snapshot rather than showing it as-is.
    Mirrors the prefill math in views.log_daily_data. Returns None if daily_log is None.
    """
    if daily_log is None:
        return None
    weeks_elapsed = (date.today() - daily_log.date).days // 7
    return daily_log.flock_age_weeks + weeks_elapsed


def assign_caging_periods(active_flock, owner, new_dates_sorted):
    """Caging-period numbers for a batch of new DailyLog dates for active_flock.

    ``new_dates_sorted`` must already be sorted ascending. Returns a list of ints, one
    per date, in the same order — the identical rule views.log_daily_data applies to a
    single new entry, just walked across a whole batch (so a CSV bulk import and a
    farmer typing rows in one at a time can never disagree on where a caging period
    boundary falls).

    Bridges from whatever history already exists for this flock: if the flock has no
    DailyLog yet, the first date in the batch continues this *owner's* overall
    caging_period counter (Max across all their flocks — see log_daily_data's docstring
    on why a flock-retirement reset must continue the counter, not restart at 1)
    unconditionally, with no gap check possible since there's no prior date to compare
    against. Otherwise it bridges from the flock's own most recent existing log. Every
    date after the first in the batch is compared to the *previous date in the batch*
    (not always the pre-existing history), so an internal gap partway through an
    imported file is detected exactly like a live gap would be.
    """
    previous_log = DailyLog.objects.filter(flock=active_flock).order_by("-date").first()
    if previous_log is None:
        max_caging_period = DailyLog.objects.filter(flock__owner=owner).aggregate(
            Max("caging_period")
        )["caging_period__max"]
        prev_date, prev_period = None, max_caging_period or 0
    else:
        prev_date, prev_period = previous_log.date, previous_log.caging_period

    periods = []
    for new_date in new_dates_sorted:
        if prev_date is None:
            new_period = prev_period + 1
        else:
            gap_days = (new_date - prev_date).days
            new_period = prev_period + 1 if gap_days > CAGING_PERIOD_GAP_DAYS else prev_period
        periods.append(new_period)
        prev_date, prev_period = new_date, new_period
    return periods


def detect_daily_log_anomalies(active_flock, cleaned_data):
    """Flag newly entered values that look far outside this flock's own history.

    Compares each field in ANOMALY_CHECK_FIELDS against the mean and spread of that
    flock's prior DailyLogs — e.g. an egg count of 700 when this flock has always
    logged around 350 gets caught here before it's saved and used to retrain the
    model. This is a soft check (surfaced to the farmer for one last look via the
    log_daily_data confirmation screen, not a hard validation error) since real farm
    conditions do genuinely shift over time and a true value shouldn't be unsaveable.

    Returns a list of human-readable warning strings; empty if nothing looks unusual,
    including when this flock doesn't have ANOMALY_MIN_HISTORY prior logs yet to judge
    what "normal" even looks like for it.
    """
    history = DailyLog.objects.filter(flock=active_flock)
    if history.count() < ANOMALY_MIN_HISTORY:
        return []

    warnings = []
    for field_name, label in ANOMALY_CHECK_FIELDS:
        past_values = [float(v) for v in history.values_list(field_name, flat=True)]
        mean = statistics.mean(past_values)
        stdev = statistics.pstdev(past_values)
        threshold = max(mean * ANOMALY_PCT_THRESHOLD, stdev * ANOMALY_STDEV_MULTIPLIER)
        new_value = float(cleaned_data[field_name])
        if threshold > 0 and abs(new_value - mean) > threshold:
            warnings.append(
                f"{label} of {cleaned_data[field_name]} is unusual for this flock — "
                f"your average so far is about {mean:.1f}."
            )
    return warnings


def resolve_trend_range(raw_value):
    """Validate a ?trend_range= GET param against TREND_RANGE_OPTIONS, defaulting to "7"."""
    return raw_value if raw_value in dict(TREND_RANGE_OPTIONS) else "7"


def build_next_day_forecasts(latest_forecast):
    """The Next 3-Day Forecast panel's 3 distinct day-by-day numbers (forecast_date +
    1/2/3), not the single predicted_tri_day_yield sum -- see forecasting/services.py's
    _predict_next_days for how these are derived. Returns [] if latest_forecast is None.
    """
    if latest_forecast is None:
        return []
    return [
        {
            "date": latest_forecast.forecast_date + timedelta(days=n),
            "value": value,
            "is_tomorrow": n == 1,
        }
        for n, value in enumerate(
            [
                latest_forecast.predicted_next_day1_yield,
                latest_forecast.predicted_next_day2_yield,
                latest_forecast.predicted_next_day3_yield,
            ],
            start=1,
        )
    ]


def build_trend_chart_data(active_flock, flock_is_caged, trend_range, next_day_forecasts):
    """Chart.js-ready trend data shared by the dashboard and the Forecast & Recommendations
    page: oldest-to-newest across the selected range of logged days, showing "actual" (from
    DailyLog) and "predicted" (from Forecast) side by side, extended with next_day_forecasts'
    dashed forward-looking tail (see build_next_day_forecasts).

    trend_range must already be validated (see resolve_trend_range). "all" has no calendar
    cutoff -- every logged day for the flock. The other two are calendar-day cutoffs, not a
    count of rows -- with the historical logging gaps documented in itikcare-spec.md
    section 10, slicing to the last N *rows* instead could silently span far more than N
    calendar days, making the label and the chart's actual date range misleading.

    Every Forecast is a same-day nowcast, so there is no genuinely future-dated Forecast row
    to pull "predicted" from beyond the logged range -- that's what next_day_forecasts
    supplies instead. trend_actual stays None for those extension points -- no DailyLog
    exists yet for a day that hasn't happened -- and the returned trend_future_start_index
    tells the caller's template where to start dashing the predicted line, so a forecast is
    never visually mistaken for a nowcast tied to a real log.
    """
    # Local import: forecasting.models only imports farm.models (not farm.services), so this
    # has no cycle, but keeping it local avoids forcing every farm.services import to also
    # resolve the forecasting app's models.
    from forecasting.models import Forecast

    if trend_range == "all":
        trend_logs = list(DailyLog.objects.filter(flock=active_flock).order_by("-date")) if flock_is_caged else []
    else:
        trend_cutoff = date.today() - timedelta(days=int(trend_range) - 1)
        trend_logs = (
            list(DailyLog.objects.filter(flock=active_flock, date__gte=trend_cutoff).order_by("-date"))
            if flock_is_caged
            else []
        )

    actual_by_date = {log.date: float(log.egg_count) for log in trend_logs}
    trend_dates = set(actual_by_date)
    predicted_by_date = {}
    if trend_dates:
        predicted_by_date = {
            f.forecast_date: float(f.predicted_daily_yield)
            for f in Forecast.objects.filter(
                flock=active_flock, forecast_date__range=(min(trend_dates), max(trend_dates))
            )
        }
    trend_dates = sorted(trend_dates)
    # (strftime's day-without-zero-padding directive isn't portable across platforms,
    # so the day number is appended manually instead of using "%-d"/"%#d".)
    trend_labels = [f"{d.strftime('%b')} {d.day}" for d in trend_dates]
    trend_actual = [actual_by_date.get(d) for d in trend_dates]
    trend_predicted = [predicted_by_date.get(d) for d in trend_dates]

    trend_future_start_index = None
    for day in next_day_forecasts:
        if day["value"] is None:
            continue
        if trend_future_start_index is None:
            trend_future_start_index = len(trend_labels)
        trend_labels.append(f"{day['date'].strftime('%b')} {day['date'].day}")
        trend_actual.append(None)
        trend_predicted.append(float(day["value"]))

    return {
        "trend_logs": trend_logs,
        "trend_labels_json": json.dumps(trend_labels),
        "trend_actual_json": json.dumps(trend_actual),
        "trend_predicted_json": json.dumps(trend_predicted),
        "trend_future_start_index": trend_future_start_index,
        "trend_has_future_forecast": trend_future_start_index is not None,
    }
