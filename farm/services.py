"""Small ORM-facing helpers shared across farm/dashboard/forecasting views.

Kept here (rather than duplicated per-view) because "the active flock" is looked up
identically in many places, and every one of those lookups must be scoped to the
requesting farmer now that more than one farm's data lives in the same tables.
"""

import json
import re
import statistics
from datetime import timedelta

from django.db.models import Max
from django.utils import timezone
from django.utils.formats import date_format
from django.utils.translation import gettext as _
from django.utils.translation import gettext_lazy

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


def recompute_caging_period(daily_log):
    """Re-derive one DailyLog's caging_period after an edit moved its date.

    caging_period is assigned once, when the row is first logged (assign_caging_periods
    above), from the day-gap to the log immediately before it. farm_record_edit audits
    a date change but that stored period can then be wrong — e.g. a date edited across
    a >CAGING_PERIOD_GAP_DAYS gap now belongs to a different free-range/caging segment.
    This recomputes just this row, from the log now immediately before it, using the
    identical gap rule.

    It deliberately does NOT cascade to later rows: a one- or two-day date correction
    is the normal case and doesn't reshuffle the series, and a farmer can't move a date
    far without the change being obvious in Farm Records. A bulk re-segmentation, if it
    were ever needed, belongs in a management command, not a request cycle.
    """
    previous_log = (
        DailyLog.objects.filter(flock=daily_log.flock, date__lt=daily_log.date)
        .exclude(pk=daily_log.pk)
        .order_by("-date")
        .first()
    )
    if previous_log is None:
        return  # now the flock's earliest entry — nothing before it to bridge from
    gap_days = (daily_log.date - previous_log.date).days
    new_period = (
        previous_log.caging_period + 1 if gap_days > CAGING_PERIOD_GAP_DAYS else previous_log.caging_period
    )
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


def build_next_day_forecasts(latest_forecast):
    """The Next 3-Day Forecast panel's 3 distinct day-by-day numbers (forecast_date +
    1/2/3), not the single predicted_tri_day_yield sum -- see forecasting/services.py's
    _predict_next_days for how these are derived. Returns [] if latest_forecast is None
    or its prediction was withheld during warm-up (see forecasting.services.forecast_readiness).
    """
    if latest_forecast is None or not latest_forecast.has_prediction:
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
    cutoff -- every logged day for the flock. "7"/"30" are calendar-day cutoffs, not a
    count of rows -- with the historical logging gaps documented in itikcare-spec.md
    section 10, slicing to the last N *rows* instead could silently span far more than N
    calendar days, making the label and the chart's actual date range misleading. A
    "YYYY-MM" value (see trend_range_choices_for/MONTH_TREND_RANGE_RE) instead pins the
    chart to one specific calendar month, same convention as farm_records' month filter.

    Every Forecast is a same-day nowcast, so there is no genuinely future-dated Forecast row
    to pull "predicted" from beyond the logged range -- that's what next_day_forecasts
    supplies instead. trend_actual stays None for those extension points -- no DailyLog
    exists yet for a day that hasn't happened -- and the returned trend_future_start_index
    tells the caller's template where to start dashing the predicted line, so a forecast is
    never visually mistaken for a nowcast tied to a real log. A specific past month is never
    extended with this tail (see is_month_view below) -- next_day_forecasts is always
    relative to *today*, so appending it to e.g. April's chart would jump straight from a
    logged month to several months in the future with nothing in between.
    """
    # Local import: forecasting.models only imports farm.models (not farm.services), so this
    # has no cycle, but keeping it local avoids forcing every farm.services import to also
    # resolve the forecasting app's models.
    from forecasting.models import Forecast

    is_month_view = bool(MONTH_TREND_RANGE_RE.match(trend_range))

    if trend_range == "all":
        trend_logs = list(DailyLog.objects.filter(flock=active_flock).order_by("-date")) if flock_is_caged else []
    elif is_month_view:
        year, month = (int(part) for part in trend_range.split("-"))
        trend_logs = (
            list(DailyLog.objects.filter(flock=active_flock, date__year=year, date__month=month).order_by("-date"))
            if flock_is_caged
            else []
        )
    else:
        trend_cutoff = timezone.localdate() - timedelta(days=int(trend_range) - 1)
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
                flock=active_flock,
                forecast_date__range=(min(trend_dates), max(trend_dates)),
                # Warm-up forecasts carry recommendations but no prediction.
                predicted_daily_yield__isnull=False,
            )
        }
    trend_dates = sorted(trend_dates)
    # (strftime's day-without-zero-padding directive isn't portable across platforms,
    # so the day number is appended manually instead of using "%-d"/"%#d".)
    trend_labels = [date_format(d, "M j") for d in trend_dates]
    trend_actual = [actual_by_date.get(d) for d in trend_dates]
    trend_predicted = [predicted_by_date.get(d) for d in trend_dates]

    trend_future_start_index = None
    for day in [] if is_month_view else next_day_forecasts:
        if day["value"] is None:
            continue
        if trend_future_start_index is None:
            trend_future_start_index = len(trend_labels)
        trend_labels.append(date_format(day["date"], "M j"))
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
