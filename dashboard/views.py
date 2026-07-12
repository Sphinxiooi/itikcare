import json
from datetime import date, timedelta

from django.shortcuts import render

from farm.models import DailyLog
from farm.services import current_flock_age_weeks, get_active_flock, get_effective_coordinates
from farm.weather import fetch_current_weather
from forecasting.models import Forecast


def index(request):
    """Root URL: public landing page for anonymous visitors, dashboard for farmers.

    Kept as one view (rather than redirecting anonymous users to a login-gated
    dashboard) so the marketing page can live at "/" without moving the dashboard
    to its own path -- see templates/landing.html.
    """

    if not request.user.is_authenticated:
        return render(request, "landing.html")

    active_flock = get_active_flock(request.user)
    # While the flock is free-range in the field (is_caged=False), nothing is being
    # logged, so no forecast/trend/records data is fetched at all — the template shows
    # a "flock is in the field" notice instead. Nothing is deleted: as soon as the
    # flock is marked caged again (Flock Profile), this data reappears untouched.
    flock_is_caged = bool(active_flock and active_flock.is_caged)

    # Live weather for the farmer's own guidance (e.g. before heading out to check on
    # the ducks) -- independent of flock/caging state, and distinct from today_log
    # below, which shows the last *submitted* DailyLog and may be stale if nothing's
    # been logged recently (itikcare-spec.md section 10's known data gaps). Never
    # raises; renders nothing if unconfigured or unreachable (see farm/weather.py).
    lat, lon = get_effective_coordinates(request.user)
    current_weather = fetch_current_weather(lat, lon)

    # "Latest" here means the soonest still-actionable forecast (today or the nearest
    # upcoming date), not the furthest-out one — that's the prediction the farmer
    # actually needs recommendations for right now. Every Forecast is a same-day
    # nowcast (see forecasting/services.py), so in practice this is today's forecast
    # or none — there's no genuinely future-dated Forecast row to prefer instead.
    latest_forecast = (
        Forecast.objects.filter(flock=active_flock, forecast_date__gte=date.today())
        .order_by("forecast_date")
        .first()
        if flock_is_caged
        else None
    )
    recommendations = latest_forecast.recommendations.all()[:3] if latest_forecast else []

    # Forecast confidence note: source_logs holds daily_log + up to 3 priors from the
    # same caging period (see forecasting/services.py's _build_feature_row) -- exactly
    # the lag1/roll3 history the prediction used. The first days of a fresh caging
    # period (right after resuming from free-range, itikcare-spec.md section 10) have
    # fewer than 3 priors, so lag1/roll3 are partly or fully imputed rather than built
    # from real recent history, and the forecast is less reliable until source_logs
    # is back up to its full count of 4.
    forecast_history_days = min(latest_forecast.source_logs.count() - 1, 3) if latest_forecast else None
    forecast_low_confidence = latest_forecast is not None and forecast_history_days < 3

    # Next 3-Day Forecast panel: 3 distinct day-by-day numbers (forecast_date + 1/2/3),
    # not the single predicted_tri_day_yield sum -- see forecasting/services.py's
    # _predict_next_days for how these are derived.
    next_day_forecasts = [
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
    ] if latest_forecast else []

    recent_logs = (
        list(DailyLog.objects.filter(flock=active_flock).order_by("-date")[:10])
        if flock_is_caged
        else []
    )
    today_log = recent_logs[0] if recent_logs else None
    # Distinct from today_log above: this is only set when the farmer has actually
    # logged *today's* data (today_log can be stale -- see note above), used for the
    # "Today's Egg Yield" card, which must stay blank until today's entry exists.
    logged_today = today_log if today_log and today_log.date == date.today() else None
    recent_records = recent_logs[:5]

    # Trend chart range: farmer-selectable via ?trend_range=, defaulting to 30 days.
    # Kept independent of recent_logs (which is capped at 10 for the status cards and
    # records table above) so widening the trend view doesn't affect those.
    TREND_RANGE_CHOICES = (7, 14, 30, 90)
    try:
        trend_range = int(request.GET.get("trend_range", 30))
    except ValueError:
        trend_range = 30
    if trend_range not in TREND_RANGE_CHOICES:
        trend_range = 30

    trend_logs = (
        list(DailyLog.objects.filter(flock=active_flock).order_by("-date")[:trend_range])
        if flock_is_caged
        else []
    )

    # Trend chart data: oldest-to-newest across the selected range of logged days,
    # showing "actual" (from DailyLog) and "predicted" (from Forecast) side by side.
    # Every Forecast is a same-day nowcast, so there is no genuinely future-dated
    # Forecast row to pull from here — the forward-looking extension below reuses
    # next_day_forecasts (predicted_next_dayN_yield) instead, see below.
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

    # Extend the line with the Next 3-Day Forecast panel's own predicted_next_dayN_yield
    # values, so the farmer can see those same forward-looking numbers plotted in context
    # next to the logged history, not just as separate cards. trend_actual stays None for
    # these -- no DailyLog exists yet for a day that hasn't happened -- and
    # trend_future_start_index tells the template where to start dashing the predicted
    # line, so a forecast is never visually mistaken for a nowcast tied to a real log.
    trend_future_start_index = None
    for day in next_day_forecasts:
        if day["value"] is None:
            continue
        if trend_future_start_index is None:
            trend_future_start_index = len(trend_labels)
        trend_labels.append(f"{day['date'].strftime('%b')} {day['date'].day}")
        trend_actual.append(None)
        trend_predicted.append(float(day["value"]))

    context = {
        "active_nav": "dashboard",
        "active_flock": active_flock,
        "flock_is_caged": flock_is_caged,
        "current_weather": current_weather,
        "today_log": today_log,
        "logged_today": logged_today,
        # Calendar-projected, not today_log.flock_age_weeks as-is -- that field is a
        # snapshot as of today_log.date, which can be stale (see today_log's note
        # above), so the "Flocks Age" card must add the weeks elapsed since then.
        "current_age_weeks": current_flock_age_weeks(today_log),
        "latest_forecast": latest_forecast,
        "next_day_forecasts": next_day_forecasts,
        "forecast_history_days": forecast_history_days,
        "forecast_low_confidence": forecast_low_confidence,
        "recommendations": recommendations,
        "recent_logs": recent_logs,
        "recent_records": recent_records,
        "trend_logs": trend_logs,
        "trend_range": trend_range,
        "trend_range_choices": TREND_RANGE_CHOICES,
        "trend_labels_json": json.dumps(trend_labels),
        "trend_actual_json": json.dumps(trend_actual),
        "trend_predicted_json": json.dumps(trend_predicted),
        "trend_future_start_index": trend_future_start_index,
        "trend_has_future_forecast": trend_future_start_index is not None,
    }
    return render(request, "dashboard/index.html", context)
