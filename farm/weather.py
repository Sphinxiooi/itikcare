"""Open-Meteo weather and geocoding lookups.

Always a best-effort suggestion (temperature/humidity stay manual entry): every function
returns None (or {}) on failure or missing config instead of raising.
"""

import logging
import re
from collections import defaultdict
from datetime import date as date_cls

import requests
from django.conf import settings
from django.core.cache import cache
from django.utils import timezone

logger = logging.getLogger(__name__)

OPEN_METEO_URL = "https://api.open-meteo.com/v1/forecast"
OPEN_METEO_ARCHIVE_URL = "https://archive-api.open-meteo.com/v1/archive"
OPEN_METEO_GEOCODING_URL = "https://geocoding-api.open-meteo.com/v1/search"
REQUEST_TIMEOUT_SECONDS = 5

# How long successful results are reused
CURRENT_WEATHER_CACHE_SECONDS = 10 * 60
FORECAST_WEATHER_CACHE_SECONDS = 60 * 60
HISTORICAL_WEATHER_CACHE_SECONDS = 6 * 60 * 60


# ---------- caching ----------
def _cached(key, timeout, fetch):
    """Return the cached value for key, else call fetch() and cache a successful result.

    Failures are not cached; cache errors are logged and treated as a miss; bypassed when
    PERFORMANCE_CACHES_ENABLED is off (tests).
    """
    if not settings.PERFORMANCE_CACHES_ENABLED:
        return fetch()

    try:
        cached = cache.get(key)
    except Exception:
        logger.warning("Weather cache read failed for key=%s", key, exc_info=True)
        cached = None
    if cached is not None:
        return cached

    result = fetch()
    if result:
        try:
            cache.set(key, result, timeout)
        except Exception:
            logger.warning("Weather cache write failed for key=%s", key, exc_info=True)
    return result


# ---------- current weather ----------
def fetch_current_weather(latitude=None, longitude=None):
    """Current {"temperature_c", "humidity_pct"} (clamped), or None; falls back to FARM_LATITUDE/LONGITUDE."""
    latitude = latitude or settings.FARM_LATITUDE
    longitude = longitude or settings.FARM_LONGITUDE
    if not latitude or not longitude:
        return None

    return _cached(
        f"weather:current:{latitude}:{longitude}",
        CURRENT_WEATHER_CACHE_SECONDS,
        lambda: _request_current_weather(latitude, longitude),
    )


def _request_current_weather(latitude, longitude):
    """Uncached request behind fetch_current_weather."""
    try:
        response = requests.get(
            OPEN_METEO_URL,
            params={
                "latitude": latitude,
                "longitude": longitude,
                "current": "temperature_2m,relative_humidity_2m",
            },
            timeout=REQUEST_TIMEOUT_SECONDS,
        )
        response.raise_for_status()
        current = response.json()["current"]
        temperature_c = round(float(current["temperature_2m"]), 1)
        humidity_pct = round(float(current["relative_humidity_2m"]), 1)
    except Exception:
        logger.warning(
            "Weather prefill fetch failed for FARM_LATITUDE=%s FARM_LONGITUDE=%s",
            latitude, longitude, exc_info=True,
        )
        return None

    # Clamp to the DailyLog form's valid ranges
    temperature_c = min(max(temperature_c, 0.0), 45.0)
    humidity_pct = min(max(humidity_pct, 0.0), 100.0)
    return {"temperature_c": temperature_c, "humidity_pct": humidity_pct}


# ---------- forecast weather ----------
def fetch_forecast_weather(latitude=None, longitude=None) -> dict:
    """{date: {"temperature_c", "humidity_pct"}} for today and the next few days; {} on failure."""
    latitude = latitude or settings.FARM_LATITUDE
    longitude = longitude or settings.FARM_LONGITUDE
    if not latitude or not longitude:
        return {}

    # Cache key includes today's date so yesterday's result isn't reused
    return _cached(
        f"weather:forecast:{latitude}:{longitude}:{timezone.localdate().isoformat()}",
        FORECAST_WEATHER_CACHE_SECONDS,
        lambda: _request_forecast_weather(latitude, longitude),
    )


def _request_forecast_weather(latitude, longitude) -> dict:
    """Uncached request behind fetch_forecast_weather (hourly values averaged per day)."""
    try:
        response = requests.get(
            OPEN_METEO_URL,
            params={
                "latitude": latitude,
                "longitude": longitude,
                "hourly": "temperature_2m,relative_humidity_2m",
                "forecast_days": 4,
                "timezone": "auto",
            },
            timeout=REQUEST_TIMEOUT_SECONDS,
        )
        response.raise_for_status()
        hourly = response.json()["hourly"]
        times = hourly["time"]
        temps = hourly["temperature_2m"]
        humidities = hourly["relative_humidity_2m"]
    except Exception:
        logger.warning(
            "Weather forecast fetch failed for FARM_LATITUDE=%s FARM_LONGITUDE=%s",
            latitude, longitude, exc_info=True,
        )
        return {}

    temps_by_date = defaultdict(list)
    humidities_by_date = defaultdict(list)
    for time_str, temp, humidity in zip(times, temps, humidities):
        if temp is None or humidity is None:
            continue
        day = date_cls.fromisoformat(time_str.split("T")[0])
        temps_by_date[day].append(float(temp))
        humidities_by_date[day].append(float(humidity))

    forecast = {}
    for day in temps_by_date:
        if day not in humidities_by_date:
            continue
        temperature_c = round(sum(temps_by_date[day]) / len(temps_by_date[day]), 1)
        humidity_pct = round(sum(humidities_by_date[day]) / len(humidities_by_date[day]), 1)
        temperature_c = min(max(temperature_c, 0.0), 45.0)
        humidity_pct = min(max(humidity_pct, 0.0), 100.0)
        forecast[day] = {"temperature_c": temperature_c, "humidity_pct": humidity_pct}

    return forecast


# ---------- historical weather ----------
def fetch_historical_weather(target_date, latitude=None, longitude=None):
    """Average temperature/humidity for one past date (archive first, then forecast endpoint), or None.

    Returns None for today/future dates or when coordinates are missing.
    """
    latitude = latitude or settings.FARM_LATITUDE
    longitude = longitude or settings.FARM_LONGITUDE
    if not latitude or not longitude or target_date >= date_cls.today():
        return None

    def request_both_tiers():
        # The archive lags a few days, so fall back to the forecast endpoint
        result = _fetch_daily_average(OPEN_METEO_ARCHIVE_URL, latitude, longitude, target_date)
        if result is None:
            result = _fetch_daily_average(OPEN_METEO_URL, latitude, longitude, target_date)
        return result

    return _cached(
        f"weather:historical:{latitude}:{longitude}:{target_date.isoformat()}",
        HISTORICAL_WEATHER_CACHE_SECONDS,
        request_both_tiers,
    )


def _fetch_daily_average(url, latitude, longitude, target_date):
    """Average one date's hourly readings from an Open-Meteo endpoint; None on failure or no data."""
    try:
        response = requests.get(
            url,
            params={
                "latitude": latitude,
                "longitude": longitude,
                "hourly": "temperature_2m,relative_humidity_2m",
                "start_date": target_date.isoformat(),
                "end_date": target_date.isoformat(),
                "timezone": "auto",
            },
            timeout=REQUEST_TIMEOUT_SECONDS,
        )
        response.raise_for_status()
        hourly = response.json()["hourly"]
        temps = [float(t) for t in hourly["temperature_2m"] if t is not None]
        humidities = [float(h) for h in hourly["relative_humidity_2m"] if h is not None]
    except Exception:
        logger.warning(
            "Historical weather fetch failed for url=%s date=%s", url, target_date, exc_info=True,
        )
        return None

    if not temps or not humidities:
        return None

    temperature_c = round(sum(temps) / len(temps), 1)
    humidity_pct = round(sum(humidities) / len(humidities), 1)
    temperature_c = min(max(temperature_c, 0.0), 45.0)
    humidity_pct = min(max(humidity_pct, 0.0), 100.0)
    return {"temperature_c": temperature_c, "humidity_pct": humidity_pct}


# ---------- geocoding ----------
def geocode_address(address):
    """Resolve a free-text farm address to (latitude, longitude), or None.

    Each word is searched separately (Philippines only); a match counts only if another
    word in the address appears in its region/province/municipality, so ambiguous names
    like "Patag" are not guessed.
    """
    if not address:
        return None

    words = [w for w in re.split(r"[,\s]+", address.strip()) if w and not w.isdigit()]
    if not words:
        return None

    best_result = None
    best_score = -1
    for index, word in enumerate(words):
        other_words = [w.lower() for i, w in enumerate(words) if i != index]
        try:
            response = requests.get(
                OPEN_METEO_GEOCODING_URL,
                params={"name": word, "count": 10, "countryCode": "PH"},
                timeout=REQUEST_TIMEOUT_SECONDS,
            )
            response.raise_for_status()
            results = response.json().get("results") or []
        except Exception:
            logger.warning("Geocoding failed for word=%r (address=%r)", word, address, exc_info=True)
            continue

        for result in results:
            admin_text = " ".join(filter(None, [
                result.get("admin1"), result.get("admin2"), result.get("admin3"),
            ])).lower()
            if other_words:
                score = sum(1 for w in other_words if w in admin_text)
            else:
                # Single-word address: trust it only if it is the sole match
                score = 1 if len(results) == 1 else 0
            if score > best_score:
                best_score = score
                best_result = result

    if best_result is None or best_score <= 0:
        return None

    return float(best_result["latitude"]), float(best_result["longitude"])
