"""Shared helpers for farm/dashboard/forecasting views: flock lookup, caging periods, anomalies, chart data."""

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

# ---------- trend chart ranges ----------
# Dropdown options shared by the dashboard and Forecast page (months are added per flock)
TREND_RANGE_OPTIONS = (
    ("7", gettext_lazy("Last 7 days")),
    ("30", gettext_lazy("Last 1 month")),
    ("all", gettext_lazy("All data")),
)

# Matches a month option like "2026-09"
MONTH_TREND_RANGE_RE = re.compile(r"^\d{4}-\d{2}$")


def trend_range_choices_for(flock):
    """TREND_RANGE_OPTIONS plus one option per month of the flock's logs (newest first)."""
    if flock is None:
        return TREND_RANGE_OPTIONS
    month_values = DailyLog.objects.filter(flock=flock).dates("date", "month", order="DESC")
    month_options = tuple((d.strftime("%Y-%m"), date_format(d, "F Y")) for d in month_values)
    return TREND_RANGE_OPTIONS + month_options

# ---------- anomaly detection settings ----------
# Minimum prior logs before a flock's own history is a usable baseline
ANOMALY_MIN_HISTORY = 5

# Flag a value beyond this fraction of the mean OR this many std devs (whichever is looser)
ANOMALY_PCT_THRESHOLD = 0.4
ANOMALY_STDEV_MULTIPLIER = 2.5

# (DailyLog field, label used in the warning text)
ANOMALY_CHECK_FIELDS = [
    ("egg_count", gettext_lazy("Egg count")),
    ("feed_intake_kg", gettext_lazy("Feed intake")),
    ("flock_size", gettext_lazy("Flock size")),
    ("temperature_c", gettext_lazy("Temperature")),
    ("humidity_pct", gettext_lazy("Humidity")),
]


# ---------- flock & date helpers ----------
def get_active_flock(owner):
    """The farmer's active Flock, or None."""
    return Flock.objects.filter(owner=owner, is_active=True).order_by("-generation_number").first()


def get_effective_coordinates(owner):
    """The farmer's own (latitude, longitude), or (None, None) to use the settings fallback."""
    if owner.latitude is not None and owner.longitude is not None:
        return owner.latitude, owner.longitude
    return None, None


# Logging day rolls over at plain midnight local time
FARM_DAY_START_HOUR = 0


def operational_today(now=None):
    """The farm's current logging day (local date); `now` can be pinned in tests."""
    now = now or timezone.localtime()
    if now.hour < FARM_DAY_START_HOUR:
        return now.date() - timedelta(days=1)
    return now.date()


def current_flock_age_weeks(daily_log):
    """Project a log's flock_age_weeks forward to today; None if there is no log."""
    if daily_log is None:
        return None
    weeks_elapsed = (timezone.localdate() - daily_log.date).days // 7
    return daily_log.flock_age_weeks + weeks_elapsed


# ---------- caging periods ----------
def caging_period_for_new_log(active_flock, owner):
    """caging_period for the flock's next DailyLog.

    First log of a flock starts a new period (continuing the owner's counter); a new
    period otherwise starts only after a free-range return. A plain logging gap keeps
    the same period.
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
    """Re-fit one log's caging_period after its date was edited (clamped between its neighbours)."""
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


# ---------- anomaly detection ----------
def detect_daily_log_anomalies(active_flock, cleaned_data):
    """Return warning strings for entered values far outside the flock's own history (soft check)."""
    warnings = []

    # More eggs than ducks is impossible; checked even on a flock's first entry
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
    """Validate ?trend_range= against the choices, defaulting to "7"."""
    return raw_value if raw_value in dict(choices) else "7"


# ---------- forecast lookups ----------
# Days ahead one Forecast predicts (next_day1/2/3)
FORECAST_HORIZON_DAYS = 3

# Forecast field for each horizon (0 = same-day prediction)
FORECAST_FIELD_BY_HORIZON = {
    0: "predicted_daily_yield",
    1: "predicted_next_day1_yield",
    2: "predicted_next_day2_yield",
    3: "predicted_next_day3_yield",
}


def predictions_by_date(flock, start, end):
    """Newest available prediction for each date in [start, end].

    Returns {date: {"value", "forecast_date", "horizon"}}; the newest Forecast wins and
    warm-up Forecasts (no prediction) are skipped. forecast_date/horizon keep it traceable.
    """
    # Local import avoids a circular import (forecasting depends on farm)
    from forecasting.models import Forecast

    forecasts = Forecast.objects.filter(
        flock=flock,
        forecast_date__range=(start - timedelta(days=FORECAST_HORIZON_DAYS), end),
        predicted_daily_yield__isnull=False,
    ).order_by("forecast_date")

    predictions = {}
    # Oldest first so a newer Forecast overwrites an older one
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
    """Next 3-Day Forecast panel: newest prediction for each not-yet-logged day, today to today + 3."""
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


# Days covered by one Tri-day Yield prediction
TRI_DAY_WINDOW_DAYS = 3


def build_tri_day_forecast(flock, today):
    """Tri-day 3-day total (value, start, end, forecast_date) or None if there is nothing to show."""
    # Local import avoids a circular import (forecasting depends on farm)
    from forecasting.models import Forecast

    # None right after a free-range return: a projection must not cross a caging boundary
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


# ---------- trend chart data ----------
def _prediction_source_label(prediction):
    """Tooltip text naming which Forecast a trend point came from."""
    if prediction["horizon"] == 0:
        return _("Same-day prediction")
    return ngettext(
        "Forecast from %(date)s (%(days)s day ahead)",
        "Forecast from %(date)s (%(days)s days ahead)",
        prediction["horizon"],
    ) % {"date": date_format(prediction["forecast_date"], "M j"), "days": prediction["horizon"]}


def build_trend_chart_data(active_flock, flock_is_caged, trend_range):
    """Chart.js trend data (actual vs predicted, oldest to newest) for the dashboard and Forecast page.

    trend_range is "7"/"30" (calendar days), "all", or "YYYY-MM". Unlogged dates with a
    prediction are shown as dashed projections, but never across a caging-period boundary.
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
        # Caging period of every log a shown prediction could depend on
        period_by_date = dict(
            flock_logs.filter(date__gte=start - timedelta(days=FORECAST_HORIZON_DAYS))
            .order_by("date")
            .values_list("date", "caging_period")
        )
        log_dates = sorted(period_by_date)

        def projection_is_shown(day, prediction):
            next_log_index = bisect_right(log_dates, day)
            if next_log_index < len(log_dates):
                # Logged again later: show only if still the same caging period
                next_log_date = log_dates[next_log_index]
                return period_by_date[next_log_date] == period_by_date.get(prediction["forecast_date"])
            # Nothing logged since: live tail, shown while the forecast is recent
            return prediction["forecast_date"] >= today - timedelta(days=FORECAST_HORIZON_DAYS)

        predicted_by_date = {
            day: prediction
            for day, prediction in predictions_by_date(active_flock, start, end).items()
            if day in actual_by_date or projection_is_shown(day, prediction)
        }

    trend_dates = sorted(set(actual_by_date) | set(predicted_by_date))
    trend_labels = [date_format(d, "M j") for d in trend_dates]
    trend_actual = [actual_by_date.get(d) for d in trend_dates]
    trend_predicted = [
        float(predicted_by_date[d]["value"]) if d in predicted_by_date else None for d in trend_dates
    ]
    # True where there is no log yet (prediction drawn dashed)
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


# ---------- farm records charts & summary ----------
# Minimum logs needed for charts/summary to be meaningful
RECORDS_CHART_MIN_LOGS = 2


def _feed_per_bird_grams(log):
    """Grams of feed per bird (same formula as recommendations/rules.py)."""
    return (float(log.feed_intake_kg) / log.flock_size) * 1000


def build_records_chart_data(logs):
    """Chart.js arrays for the Farm Records page, oldest to newest."""
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
    """Farm Records stat tiles: averages plus month-over-month % change (None without a previous month)."""
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
