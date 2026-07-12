import json
from datetime import date

from django.contrib.auth.decorators import login_required
from django.shortcuts import render

from farm.models import DailyLog
from farm.services import get_active_flock

from .models import Forecast

# Maps a Recommendation.triggered_by feature name to how it's grouped and labeled on
# the Forecast & Recommendations page, matching the Figma categories (Flock
# Management, Feeding Management, Humidity/Temperature Management under
# Environmental Control). Kept as a plain dict, not a model, since it's presentation
# grouping only — the traceability itself lives in Recommendation.triggered_by.
RECOMMENDATION_CATEGORIES = {
    "flock_age_weeks": {"title": "Flock Management", "icon": "🔧", "color": "amber"},
    "feed_intake_kg": {"title": "Feeding Management", "icon": "🌾", "color": "emerald"},
    "humidity_pct": {"title": "Humidity Management", "icon": "💧", "color": "blue"},
    "temperature_c": {"title": "Temperature Management", "icon": "🌡️", "color": "red"},
}


@login_required
def forecast_recommendations(request):
    """Combined Forecast + Recommendations page (Figma's two-tab screen).

    Both tabs are rendered server-side in one page (tab switching is pure CSS/JS,
    no extra request) since the data for both comes from the same latest_forecast.
    """

    active_flock = get_active_flock(request.user)
    # While the flock is free-range in the field (is_caged=False), no forecast/trend
    # data is fetched — see dashboard.views.index for the same gating and reasoning.
    flock_is_caged = bool(active_flock and active_flock.is_caged)

    # "Latest" means the soonest still-actionable forecast, not the furthest-out one
    # — see dashboard.views.index for the same convention and reasoning.
    latest_forecast = (
        Forecast.objects.filter(flock=active_flock, forecast_date__gte=date.today())
        .order_by("forecast_date")
        .first()
        if flock_is_caged
        else None
    )
    upcoming_forecasts = (
        Forecast.objects.filter(flock=active_flock, forecast_date__gte=date.today())
        .order_by("forecast_date")[:3]
        if flock_is_caged
        else []
    )
    recent_logs = (
        list(DailyLog.objects.filter(flock=active_flock).order_by("-date")[:10])
        if flock_is_caged
        else []
    )
    trend_labels = [f"{log.date.strftime('%b')} {log.date.day}" for log in reversed(recent_logs)]
    trend_actual = [float(log.egg_count) for log in reversed(recent_logs)]

    feature_importances = []
    grouped_recommendations = []
    if latest_forecast:
        # Sort by importance descending so the dashboard/thesis narrative — "the
        # highest-importance negative factor is flagged first" — is visible here too.
        feature_importances = sorted(
            latest_forecast.feature_importances.items(), key=lambda item: item[1], reverse=True
        )

        recs_by_feature = {}
        for rec in latest_forecast.recommendations.all():
            recs_by_feature.setdefault(rec.triggered_by, []).append(rec)

        for feature_name, importance in feature_importances:
            recs = recs_by_feature.get(feature_name)
            if not recs:
                continue
            meta = RECOMMENDATION_CATEGORIES.get(feature_name, {"title": feature_name, "icon": "📌", "color": "gray"})
            grouped_recommendations.append({"meta": meta, "recommendations": recs})

    context = {
        "active_nav": "forecast",
        "active_flock": active_flock,
        "flock_is_caged": flock_is_caged,
        "latest_forecast": latest_forecast,
        "upcoming_forecasts": upcoming_forecasts,
        "feature_importances": feature_importances,
        "grouped_recommendations": grouped_recommendations,
        "trend_labels_json": json.dumps(trend_labels),
        "trend_actual_json": json.dumps(trend_actual),
    }
    return render(request, "forecasting/forecast_recommendations.html", context)
