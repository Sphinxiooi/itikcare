from django.contrib.auth.decorators import login_required
from django.shortcuts import render

from farm.services import (
    build_next_day_forecasts,
    build_trend_chart_data,
    get_active_flock,
    operational_today,
    resolve_trend_range,
    trend_range_choices_for,
)

from recommendations import rules as recommendation_rules

from .models import Forecast
from .services import current_forecast_warmup
from .pipeline import FEATURE_LABELS, FEATURES as RAW_FEATURES
from django.utils.translation import gettext as _
from django.utils.translation import gettext_lazy

# Maps a Recommendation.triggered_by key to how it's grouped and labeled on the Forecast &
# Recommendations page, matching the Figma categories (Flock Management, Feeding
# Management, Temperature/Humidity Management). Kept as a plain dict, not a model, since
# it's presentation grouping only — the traceability itself lives in
# Recommendation.triggered_by.
RECOMMENDATION_CATEGORIES = {
    "flock_age_weeks": {"title": gettext_lazy("Flock Management"), "icon": "wrench", "color": "amber"},
    "feed_intake_kg": {"title": gettext_lazy("Feeding Management"), "icon": "wheat", "color": "emerald"},
    "temperature_c": {"title": gettext_lazy("Temperature Management"), "icon": "thermometer", "color": "red"},
    "humidity_pct": {"title": gettext_lazy("Humidity Management"), "icon": "droplet", "color": "blue"},
}

# These two triggered_by keys render nested inside one outer "Environmental Management"
# frame (Figma) instead of each getting its own top-level card — see the grouping loop in
# forecast_recommendations() below. Kept as a literal tuple (not a generic "groups of
# groups" abstraction) since this is thesis code that needs to be walked through in a
# defense — a reviewer asking "why do these two nest together" is better answered by this
# one line than by an extra layer of indirection.
ENVIRONMENTAL_KEYS = ("temperature_c", "humidity_pct")
ENVIRONMENT_FRAME_META = {"title": gettext_lazy("Environmental Management"), "icon": "cloud", "color": "gray"}


def _percent_shares(importances):
    """Rescale {name: importance} to whole-number percents that sum to exactly 100.

    Returns [(name, percent), ...] sorted by percent descending. Uses the largest-
    remainder method: floor every share, then hand the leftover points to the entries
    with the biggest fractional parts (ties go to the more important feature), so
    rounding never leaves the panel at 99% or 101%.
    """
    total = sum(importances.values())
    if total <= 0:
        return []
    exact = {name: value / total * 100 for name, value in importances.items()}
    percents = {name: int(share) for name, share in exact.items()}
    leftover = 100 - sum(percents.values())
    by_remainder = sorted(
        exact,
        # Rounded so float noise can't break a genuine tie in the fractional parts.
        key=lambda name: (round(exact[name] - percents[name], 9), exact[name]),
        reverse=True,
    )
    for name in by_remainder[:leftover]:
        percents[name] += 1
    return sorted(percents.items(), key=lambda item: item[1], reverse=True)


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
        Forecast.objects.filter(flock=active_flock, forecast_date__gte=operational_today())
        .order_by("forecast_date")
        .first()
        if flock_is_caged
        else None
    )
    # Next 3-Day Forecast panel data: the recursive day+1/2/3 projection (see
    # farm.services.build_next_day_forecasts), shared with the dashboard's identical
    # panel and also used to extend the Egg Yield Trend chart's predicted line with a
    # dashed forward-looking tail. This page used to instead query stored Forecast rows
    # directly (forecast_date >= today), but every Forecast row is a same-day nowcast, so
    # that query almost never returned more than 1 distinct future day -- next_day_forecasts
    # is the correct source for a genuine 3-day-ahead view.
    next_day_forecasts = build_next_day_forecasts(latest_forecast)
    # Prediction withheld during warm-up (recommendations still show) -- see
    # forecasting.services.forecast_readiness.
    forecast_warmup = current_forecast_warmup(active_flock)
    trend_range_choices = trend_range_choices_for(active_flock)
    trend_range = resolve_trend_range(request.GET.get("trend_range", "7"), trend_range_choices)
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
        # The RF importances sum to 1 across all 7 MODEL_FEATURES, so the 5 shown here
        # cover less than 100% on their own. They're rescaled to share 100% among
        # themselves (each factor's share of the farmer-controllable importance) and
        # rounded to whole percents that add up to exactly 100. Recommendation ordering
        # still uses the unscaled importances (recommendation_rules.importance_for).
        raw_importances = {
            name: importance
            for name, importance in latest_forecast.feature_importances.items()
            if name in RAW_FEATURES
        }
        feature_importances = [
            # pipeline.py stays Django-free (no translation imports), so its English
            # labels are translated here, at display time.
            (_(FEATURE_LABELS.get(name, name)), percent)
            for name, percent in _percent_shares(raw_importances)
        ]

        recs_by_feature = {}
        for rec in latest_forecast.recommendations.all():
            recs_by_feature.setdefault(rec.triggered_by, []).append(rec)

        fi = latest_forecast.feature_importances

        def _card(key):
            meta = RECOMMENDATION_CATEGORIES.get(key, {"title": key, "icon": "tag", "color": "gray"})
            return {"meta": meta, "recommendations": recs_by_feature[key]}

        # Non-environmental keys each become their own top-level card; temperature_c and
        # humidity_pct (if present) collapse into one "Environmental Management" block
        # containing both as sub-cards -- see ENVIRONMENTAL_KEYS above.
        blocks = [
            {"sort_key": recommendation_rules.importance_for(key, fi), "kind": "single", "card": _card(key)}
            for key in recs_by_feature
            if key not in ENVIRONMENTAL_KEYS
        ]
        env_keys = [key for key in recs_by_feature if key in ENVIRONMENTAL_KEYS]
        if env_keys:
            env_keys.sort(key=lambda key: recommendation_rules.importance_for(key, fi), reverse=True)
            blocks.append({
                "sort_key": max(recommendation_rules.importance_for(key, fi) for key in env_keys),
                "kind": "environment",
                "meta": ENVIRONMENT_FRAME_META,
                "sub_cards": [_card(key) for key in env_keys],
            })

        blocks.sort(key=lambda block: block["sort_key"], reverse=True)
        grouped_recommendations = blocks

    context = {
        "active_nav": "forecast",
        "active_flock": active_flock,
        "flock_is_caged": flock_is_caged,
        "latest_forecast": latest_forecast,
        "next_day_forecasts": next_day_forecasts,
        "forecast_warmup": forecast_warmup,
        "feature_importances": feature_importances,
        "grouped_recommendations": grouped_recommendations,
        "trend_range": trend_range,
        "trend_range_label": dict(trend_range_choices)[trend_range],
        "trend_range_choices": trend_range_choices,
        **trend_data,
    }
    return render(request, "forecasting/forecast_recommendations.html", context)
