from datetime import date

from django.shortcuts import render

from farm.models import DailyLog
from farm.services import (
    TREND_RANGE_OPTIONS,
    build_next_day_forecasts,
    build_trend_chart_data,
    current_flock_age_weeks,
    get_active_flock,
    get_effective_coordinates,
    resolve_trend_range,
)
from farm.weather import fetch_current_weather
from forecasting.models import Forecast
from recommendations.models import Recommendation

# Lower rank = shown first. Independent of feature-importance order -- the dashboard's
# single "Quick Recommendation" card is about urgency (what needs attention right now),
# not which feature the model currently weighs most; the full importance-ordered list
# lives on the Forecast & Recommendations page (forecasting/views.py).
PRIORITY_RANK = {Recommendation.Priority.HIGH: 0, Recommendation.Priority.MEDIUM: 1, Recommendation.Priority.LOW: 2}


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
    # Dashboard shows only the single most urgent recommendation (highest priority,
    # ties broken by feature importance -- matching the ordering convention on the
    # Forecast & Recommendations page). Every fired-rule feature always has exactly one
    # Recommendation now (recommendations/rules.py fires a LOW "all good" confirmation
    # when nothing's wrong), so this always picks the one thing most worth surfacing
    # here; the full set is on the Forecast & Recommendations page, not truncated there.
    top_recommendation = None
    if latest_forecast:
        all_recs = list(latest_forecast.recommendations.all())
        if all_recs:
            importance_rank = {
                feature: rank
                for rank, feature in enumerate(
                    sorted(
                        latest_forecast.feature_importances,
                        key=latest_forecast.feature_importances.get,
                        reverse=True,
                    )
                )
            }
            top_recommendation = min(
                all_recs,
                key=lambda r: (PRIORITY_RANK.get(r.priority, 99), importance_rank.get(r.triggered_by, 99)),
            )

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
    # not the single predicted_tri_day_yield sum -- see farm.services.build_next_day_forecasts
    # (shared with the Forecast & Recommendations page's trend chart, see below).
    next_day_forecasts = build_next_day_forecasts(latest_forecast)

    recent_logs = (
        list(DailyLog.objects.filter(flock=active_flock).order_by("-date")[:10])
        if flock_is_caged
        else []
    )
    today_log = recent_logs[0] if recent_logs else None
    # Fall back to the pending_* values staged on the flock at registration/resume-
    # caging time when no DailyLog exists yet at all, so a freshly registered flock's
    # stats cards show real numbers immediately instead of "—" until the first entry.
    flock_size_display = today_log.flock_size if today_log else (active_flock.pending_flock_size if active_flock else None)
    feed_intake_display = today_log.feed_intake_kg if today_log else (active_flock.pending_feed_intake_kg if active_flock else None)
    # Distinct from today_log above: this is only set when the farmer has actually
    # logged *today's* data (today_log can be stale -- see note above), used for the
    # "Today's Egg Yield" card, which must stay blank until today's entry exists.
    logged_today = today_log if today_log and today_log.date == date.today() else None
    recent_records = recent_logs[:5]

    # Trend chart range: farmer-selectable via ?trend_range=, defaulting to 7 days.
    # Kept independent of recent_logs (which is capped at 10 for the status cards and
    # records table above) so widening the trend view doesn't affect those. Shared with
    # the Forecast & Recommendations page's own trend chart -- see farm.services.
    trend_range = resolve_trend_range(request.GET.get("trend_range", "7"))
    trend_data = build_trend_chart_data(active_flock, flock_is_caged, trend_range, next_day_forecasts)

    context = {
        "active_nav": "dashboard",
        "active_flock": active_flock,
        "flock_is_caged": flock_is_caged,
        "current_weather": current_weather,
        "today_log": today_log,
        "logged_today": logged_today,
        "flock_size_display": flock_size_display,
        "feed_intake_display": feed_intake_display,
        # Calendar-projected, not today_log.flock_age_weeks as-is -- that field is a
        # snapshot as of today_log.date, which can be stale (see today_log's note
        # above), so the "Flocks Age" card must add the weeks elapsed since then.
        # Falls back to pending_flock_age_weeks (confirmed at registration/resume-caging)
        # when no DailyLog has been logged yet at all, so a freshly registered flock
        # shows its real age immediately instead of "—" until the first entry.
        "current_age_weeks": current_flock_age_weeks(today_log) or (active_flock.pending_flock_age_weeks if active_flock else None),
        "latest_forecast": latest_forecast,
        "next_day_forecasts": next_day_forecasts,
        "forecast_history_days": forecast_history_days,
        "forecast_low_confidence": forecast_low_confidence,
        "top_recommendation": top_recommendation,
        "recent_logs": recent_logs,
        "recent_records": recent_records,
        "trend_range": trend_range,
        "trend_range_label": dict(TREND_RANGE_OPTIONS)[trend_range],
        "trend_range_choices": TREND_RANGE_OPTIONS,
        **trend_data,
    }
    return render(request, "dashboard/index.html", context)
