import json
from datetime import date

from django.contrib.auth.decorators import login_required
from django.shortcuts import render

from farm.models import DailyLog, Flock
from forecasting.models import Forecast


@login_required
def index(request):
    """Main landing page: greeting, KPI cards, 3-day forecast, trend chart, quick tips.

    This mirrors the Figma dashboard screen. No real Forecast/Recommendation data
    exists yet since the RF training pipeline and rule engine are both still stubs,
    so every section degrades gracefully to an empty/placeholder state.
    """

    active_flock = Flock.objects.filter(is_active=True).order_by("-generation_number").first()
    # "Latest" here means the soonest still-actionable forecast (today or the nearest
    # upcoming date), not the furthest-out one — that's the prediction the farmer
    # actually needs recommendations for right now.
    latest_forecast = (
        Forecast.objects.filter(flock=active_flock, forecast_date__gte=date.today())
        .order_by("forecast_date")
        .first()
        if active_flock
        else None
    )
    upcoming_forecasts = (
        Forecast.objects.filter(flock=active_flock, forecast_date__gte=date.today()).order_by("forecast_date")[:3]
        if active_flock
        else []
    )
    recommendations = latest_forecast.recommendations.all()[:3] if latest_forecast else []
    recent_logs = (
        list(DailyLog.objects.filter(flock=active_flock).order_by("-date")[:10])
        if active_flock
        else []
    )
    today_log = recent_logs[0] if recent_logs else None
    recent_records = recent_logs[:5]

    # Trend chart data: oldest-to-newest across the last 10 logged days plus the next
    # 3 forecast days, so the chart can show "actual" (from DailyLog) and "predicted"
    # (from Forecast) side by side, matching the Figma two-line chart. Predicted
    # values are only available where a Forecast row happens to exist for that date
    # — expected to be mostly gaps until the RF pipeline is wired up for real.
    actual_by_date = {log.date: float(log.egg_count) for log in recent_logs}
    trend_dates = set(actual_by_date) | {f.forecast_date for f in upcoming_forecasts}
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
        "today_log": today_log,
        "latest_forecast": latest_forecast,
        "upcoming_forecasts": upcoming_forecasts,
        "recommendations": recommendations,
        "recent_logs": recent_logs,
        "recent_records": recent_records,
        "trend_labels_json": json.dumps(trend_labels),
        "trend_actual_json": json.dumps(trend_actual),
        "trend_predicted_json": json.dumps(trend_predicted),
    }
    return render(request, "dashboard/index.html", context)
