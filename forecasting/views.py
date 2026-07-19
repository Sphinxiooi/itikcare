from datetime import date

from django.contrib.auth.decorators import login_required
from django.shortcuts import render

from farm.services import (
    TREND_RANGE_OPTIONS,
    build_next_day_forecasts,
    build_trend_chart_data,
    get_active_flock,
    resolve_trend_range,
)

from .models import Forecast
from .pipeline import FEATURES as RAW_FEATURES

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
    # Next 3-Day Forecast panel data used only to extend the Egg Yield Trend chart's
    # predicted line with a dashed forward-looking tail -- shared with the dashboard's
    # own trend chart, see farm.services. Distinct from upcoming_forecasts above, which
    # drives this page's own "Next 3-day Forecast" cards from separately stored Forecast
    # rows rather than this recursive same-forecast projection.
    next_day_forecasts = build_next_day_forecasts(latest_forecast)
    trend_range = resolve_trend_range(request.GET.get("trend_range", "7"))
    trend_data = build_trend_chart_data(active_flock, flock_is_caged, trend_range, next_day_forecasts)

    feature_importances = []
    grouped_recommendations = []
    if latest_forecast:
        # Sort by importance descending so the dashboard/thesis narrative — "the
        # highest-importance negative factor is flagged first" — is visible here too.
        # Filtered to the 5 spec-named farmer inputs (itikcare-spec.md section 4) --
        # the model's own lag1/roll3 history features are real inputs to the RF model
        # (see forecasting/pipeline.py's MODEL_FEATURES) but aren't something a farmer
        # entered or can act on, so they're excluded from this farmer-facing panel.
        # Values are RF fractions summing to 1 across all 7 MODEL_FEATURES, not just
        # these 5, so they're converted to percent but intentionally NOT rescaled to
        # sum to 100 among themselves -- each percentage is the feature's true,
        # unadjusted share of the model's total importance.
        feature_importances = sorted(
            (
                (name, importance * 100)
                for name, importance in latest_forecast.feature_importances.items()
                if name in RAW_FEATURES
            ),
            key=lambda item: item[1], reverse=True,
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
        "trend_range": trend_range,
        "trend_range_label": dict(TREND_RANGE_OPTIONS)[trend_range],
        "trend_range_choices": TREND_RANGE_OPTIONS,
        **trend_data,
    }
    return render(request, "forecasting/forecast_recommendations.html", context)
