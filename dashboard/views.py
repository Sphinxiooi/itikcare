from django.http import HttpResponse
from django.shortcuts import render
from django.urls import reverse

from farm.models import DailyLog
from farm.services import (
    build_next_day_forecasts,
    build_trend_chart_data,
    current_flock_age_weeks,
    get_active_flock,
    get_effective_coordinates,
    operational_today,
    resolve_trend_range,
    trend_range_choices_for,
)
from farm.weather import fetch_current_weather
from forecasting.models import Forecast
from forecasting.services import current_forecast_warmup, current_logging_gap_caution
from recommendations.services import ACTIONABLE_PRIORITIES, most_urgent_recommendation


# ---------- public pages ----------
def researchers(request):
    """Public "About the Researchers" page."""

    return render(request, "researchers.html")


def privacy_notice(request):
    """Public Privacy Notice (Data Privacy Act of 2012)."""

    return render(request, "privacy.html")


def robots_txt(request):
    """robots.txt: allow crawling and point to the sitemap."""

    sitemap_url = request.build_absolute_uri(reverse("sitemap_xml"))
    return HttpResponse(f"User-agent: *\nAllow: /\nSitemap: {sitemap_url}\n", content_type="text/plain")


def sitemap_xml(request):
    """Sitemap listing only the publicly reachable pages."""

    page_urls = [
        request.build_absolute_uri(reverse("dashboard")),
        request.build_absolute_uri(reverse("researchers")),
        request.build_absolute_uri(reverse("privacy_notice")),
    ]
    url_entries = "".join(f"<url><loc>{url}</loc></url>" for url in page_urls)
    xml = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
        f"{url_entries}"
        "</urlset>"
    )
    return HttpResponse(xml, content_type="application/xml")


def google_site_verification(request):
    """Google Search Console site-verification file."""

    return HttpResponse(
        "google-site-verification: google6c0aee7b83489d73.html",
        content_type="text/plain",
    )


# ---------- dashboard ----------
def index(request):
    """Landing page for visitors, dashboard for logged-in farmers."""

    if not request.user.is_authenticated:
        return render(request, "landing.html")

    active_flock = get_active_flock(request.user)
    # Free-range flock: no forecast/trend data is fetched (nothing is deleted)
    flock_is_caged = bool(active_flock and active_flock.is_caged)

    # Live weather card (never raises; empty if unconfigured/unreachable)
    lat, lon = get_effective_coordinates(request.user)
    current_weather = fetch_current_weather(lat, lon)

    # Soonest still-actionable forecast (today or later); drives the Quick Recommendation card
    latest_forecast = (
        Forecast.objects.filter(flock=active_flock, forecast_date__gte=operational_today())
        .order_by("forecast_date")
        .first()
        if flock_is_caged
        else None
    )
    # Show only the single most urgent recommendation, and only at an actionable priority
    top_recommendation = None
    has_calm_recommendations = False
    most_urgent = most_urgent_recommendation(latest_forecast)
    if most_urgent is not None:
        if most_urgent.priority in ACTIONABLE_PRIORITIES:
            top_recommendation = most_urgent
        else:
            has_calm_recommendations = True

    # Warm-up notice (prediction withheld) and logging-gap caution (prediction less accurate)
    forecast_warmup = current_forecast_warmup(active_flock)
    logging_gap_caution = None if forecast_warmup else current_logging_gap_caution(active_flock)

    # Next 3-Day Forecast panel
    next_day_forecasts = build_next_day_forecasts(active_flock, operational_today()) if flock_is_caged else []

    recent_logs = (
        list(DailyLog.objects.filter(flock=active_flock).order_by("-date")[:10])
        if flock_is_caged
        else []
    )
    today_log = recent_logs[0] if recent_logs else None
    # Fall back to staged values so a brand-new flock shows real numbers before its first log
    flock_size_display = today_log.flock_size if today_log else (active_flock.pending_flock_size if active_flock else None)
    feed_intake_display = today_log.feed_intake_kg if today_log else (active_flock.pending_feed_intake_kg if active_flock else None)
    # Set only if today's data is actually logged (today_log may be stale)
    logged_today = today_log if today_log and today_log.date == operational_today() else None
    recent_records = recent_logs[:5]

    # Trend chart range (?trend_range=, default 7 days)
    trend_range_choices = trend_range_choices_for(active_flock)
    trend_range = resolve_trend_range(request.GET.get("trend_range", "7"), trend_range_choices)
    trend_data = build_trend_chart_data(active_flock, flock_is_caged, trend_range)

    context = {
        "active_nav": "dashboard",
        "active_flock": active_flock,
        "flock_is_caged": flock_is_caged,
        "current_weather": current_weather,
        "today_log": today_log,
        "logged_today": logged_today,
        "flock_size_display": flock_size_display,
        "feed_intake_display": feed_intake_display,
        # Age projected to today, falling back to the staged age
        "current_age_weeks": current_flock_age_weeks(today_log) or (active_flock.pending_flock_age_weeks if active_flock else None),
        "latest_forecast": latest_forecast,
        "next_day_forecasts": next_day_forecasts,
        "forecast_warmup": forecast_warmup,
        "logging_gap_caution": logging_gap_caution,
        "top_recommendation": top_recommendation,
        "has_calm_recommendations": has_calm_recommendations,
        "recent_logs": recent_logs,
        "recent_records": recent_records,
        "trend_range": trend_range,
        "trend_range_label": dict(trend_range_choices)[trend_range],
        "trend_range_choices": trend_range_choices,
        **trend_data,
    }
    return render(request, "dashboard/index.html", context)
