import json
from datetime import date

from django.contrib.auth.decorators import login_required
from django.shortcuts import render

from farm.models import DailyLog, Flock
from farm.weather import fetch_current_weather
from forecasting.models import Forecast


@login_required
def index(request):
    """Main landing page: greeting, KPI cards, tri-day forecast, trend chart, quick tips.

    This mirrors the Figma dashboard screen.
    """

    active_flock = Flock.objects.filter(is_active=True).order_by("-generation_number").first()
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
    current_weather = fetch_current_weather()

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
    recent_logs = (
        list(DailyLog.objects.filter(flock=active_flock).order_by("-date")[:10])
        if flock_is_caged
        else []
    )
    today_log = recent_logs[0] if recent_logs else None
    recent_records = recent_logs[:5]

    # Trend chart data: oldest-to-newest across the last 10 logged days, showing
    # "actual" (from DailyLog) and "predicted" (from Forecast) side by side. Every
    # Forecast is a same-day nowcast, so there is no genuinely future-dated forecast
    # to extend this chart with — it only ever covers days that have already been
    # logged.
    actual_by_date = {log.date: float(log.egg_count) for log in recent_logs}
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

    context = {
        "active_nav": "dashboard",
        "active_flock": active_flock,
        "flock_is_caged": flock_is_caged,
        "current_weather": current_weather,
        "today_log": today_log,
        "latest_forecast": latest_forecast,
        "recommendations": recommendations,
        "recent_logs": recent_logs,
        "recent_records": recent_records,
        "trend_labels_json": json.dumps(trend_labels),
        "trend_actual_json": json.dumps(trend_actual),
        "trend_predicted_json": json.dumps(trend_predicted),
    }
    return render(request, "dashboard/index.html", context)
