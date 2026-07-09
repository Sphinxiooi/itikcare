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

    # Trend chart data: oldest-to-newest actual yield for the last 10 logged days.
    # (strftime's day-without-zero-padding directive isn't portable across platforms,
    # so the day number is appended manually instead of using "%-d"/"%#d".)
    trend_labels = [f"{log.date.strftime('%b')} {log.date.day}" for log in reversed(recent_logs)]
    trend_actual = [float(log.egg_count) for log in reversed(recent_logs)]

    context = {
        "active_nav": "dashboard",
        "active_flock": active_flock,
        "today_log": today_log,
        "latest_forecast": latest_forecast,
        "upcoming_forecasts": upcoming_forecasts,
        "recommendations": recommendations,
        "recent_logs": recent_logs,
        "trend_labels_json": json.dumps(trend_labels),
        "trend_actual_json": json.dumps(trend_actual),
    }
    return render(request, "dashboard/index.html", context)
