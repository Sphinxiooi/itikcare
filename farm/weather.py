"""Weather lookups — always a best-effort *suggestion*, never a substitute for the
farmer's own reading (itikcare-spec.md section 7: temperature/humidity are always
manual entry). Every function here returns None on any failure/missing config rather
than raising, so a caller can always safely treat None as "leave it to the farmer."

fetch_current_weather powers the dashboard's live-weather widget (dashboard.views.index)
— informational only, not tied to any form field.

fetch_forecast_weather estimates temperature_c/humidity_pct for the next few days when
forecasting.services recursively projects future egg yield.

fetch_historical_weather suggests a starting temperature_c/humidity_pct for a
*backdated* Log Daily Data entry (see farm.views.log_daily_data / weather_for_date) —
adviser-directed addition so a farmer backfilling a missed past day doesn't have to
remember that day's exact reading from memory. It tries Open-Meteo's Historical Weather
(reanalysis) archive first, since it covers any past date, then falls back to the
regular forecast endpoint's own start_date/end_date range — the archive lags ~2-5 days
behind real time, so a date from earlier this week may not be in it yet, while the
forecast endpoint always carries the last ~92 days. The client-side field is always
still a normal editable input the farmer can overwrite before submitting; today's date
is never looked up here (see farm.views.log_daily_data's own "today stays blank" rule).

geocode_address is a standalone concern: turning the free-text farm address a farmer
types at signup (accounts.views.signup) into the latitude/longitude the functions above
need. It's kept in this module rather than in accounts/ because it's still just "talk
to Open-Meteo," the same HTTP/timeout/logging pattern as the rest of this file, and it
means accounts/ never has to know Open-Meteo is the provider.
"""

import logging
import re
from collections import defaultdict
from datetime import date as date_cls

import requests
from django.conf import settings

logger = logging.getLogger(__name__)

OPEN_METEO_URL = "https://api.open-meteo.com/v1/forecast"
OPEN_METEO_ARCHIVE_URL = "https://archive-api.open-meteo.com/v1/archive"
OPEN_METEO_GEOCODING_URL = "https://geocoding-api.open-meteo.com/v1/search"
REQUEST_TIMEOUT_SECONDS = 5


def fetch_current_weather(latitude=None, longitude=None):
    """Best-effort fetch of current temperature (°C) / relative humidity (%) at the
    given coordinates, falling back to the global FARM_LATITUDE/FARM_LONGITUDE settings
    (the foundation farmer's location) when latitude/longitude aren't passed — see
    farm.services.get_effective_coordinates, which callers should use to resolve a
    specific farmer's own coordinates before calling this.

    Returns {"temperature_c": float, "humidity_pct": float}, rounded to 1 decimal and
    clamped to DailyLogForm's widget ranges (0-45 / 0-100), or None if coordinates
    aren't configured, the request fails/times out, or the response is malformed.
    Never raises: farm connectivity is expected to be spotty (rural, semi-intensive
    husbandry), so callers can always safely treat None as "leave the field blank for
    the farmer to fill in," same as before this existed.
    """
    latitude = latitude or settings.FARM_LATITUDE
    longitude = longitude or settings.FARM_LONGITUDE
    if not latitude or not longitude:
        return None

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

    # Clamp defensively so an edge-case API reading can never sit outside DailyLog's
    # own validators and break the very first render of the form.
    temperature_c = min(max(temperature_c, 0.0), 45.0)
    humidity_pct = min(max(humidity_pct, 0.0), 100.0)
    return {"temperature_c": temperature_c, "humidity_pct": humidity_pct}


def fetch_forecast_weather(latitude=None, longitude=None) -> dict:
    """Best-effort short-range forecast: {date: {"temperature_c": float, "humidity_pct":
    float}} for today and the next few days at the given coordinates, falling back to
    the global FARM_LATITUDE/FARM_LONGITUDE settings when latitude/longitude aren't
    passed — see fetch_current_weather's docstring for the same fallback contract.

    Keyed by real calendar date (not a "day+1/day+2" offset) so a caller anchoring off
    an arbitrary date can just do weather_by_date.get(target_date) and get nothing back
    for a date outside Open-Meteo's real-today-anchored forecast window, instead of
    silently misattributing a wrong day's forecast to it.

    Open-Meteo has no daily-aggregate humidity variable, so both fields are pulled from
    the hourly endpoint and averaged per calendar day here. forecast_days=4 (today plus
    3 full future days) so the 3rd future day's 24-hour bucket is complete rather than
    truncated. timezone=auto aligns day boundaries to the farm's local calendar day
    (matching how farmers enter DailyLog.date) instead of defaulting to GMT.

    Returns {} (never None) on any failure, so callers can always call .get(...) on the
    result unconditionally. Never raises, same contract as fetch_current_weather.
    """
    latitude = latitude or settings.FARM_LATITUDE
    longitude = longitude or settings.FARM_LONGITUDE
    if not latitude or not longitude:
        return {}

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
        # Same defensive clamp as fetch_current_weather.
        temperature_c = min(max(temperature_c, 0.0), 45.0)
        humidity_pct = min(max(humidity_pct, 0.0), 100.0)
        forecast[day] = {"temperature_c": temperature_c, "humidity_pct": humidity_pct}

    return forecast


def fetch_historical_weather(target_date, latitude=None, longitude=None):
    """Best-effort average temperature (°C) / relative humidity (%) for one *past*
    calendar date, to suggest a starting value for a backdated Log Daily Data entry
    (see farm.views.weather_for_date). Falls back to the global FARM_LATITUDE/
    FARM_LONGITUDE settings when latitude/longitude aren't passed, same contract as
    fetch_current_weather.

    Returns None for target_date being today or in the future — there's nothing
    "historical" to look up yet, and log_daily_data already leaves today's fields
    blank on its own. Also returns None if coordinates aren't configured, or if
    every fetch attempt below fails — same "None means leave the field blank for the
    farmer" contract as the other two lookups in this module.

    Tries OPEN_METEO_ARCHIVE_URL (Open-Meteo's Historical Weather / reanalysis
    archive) first, since it covers any past date, but that data lags roughly 2-5
    days behind real time — a date from earlier this week may not be in it yet, and
    that shows up as an all-null hourly series rather than an error. When that
    happens, falls back to OPEN_METEO_URL's own start_date/end_date range instead,
    which always carries the last ~92 days including yesterday.
    """
    latitude = latitude or settings.FARM_LATITUDE
    longitude = longitude or settings.FARM_LONGITUDE
    if not latitude or not longitude or target_date >= date_cls.today():
        return None

    result = _fetch_daily_average(OPEN_METEO_ARCHIVE_URL, latitude, longitude, target_date)
    if result is None:
        result = _fetch_daily_average(OPEN_METEO_URL, latitude, longitude, target_date)
    return result


def _fetch_daily_average(url, latitude, longitude, target_date):
    """Shared hourly-fetch-and-average helper behind fetch_historical_weather's two
    fallback tiers: requests hourly temperature_2m/relative_humidity_2m for a single
    calendar date from either Open-Meteo endpoint (both accept the same
    start_date/end_date/timezone params) and averages them the same way
    fetch_forecast_weather does per day. Returns None (never raises) on a request
    failure, a malformed response, or an hourly series with no non-null readings at
    all for that date (e.g. the archive endpoint hasn't caught up to target_date yet).
    """
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
    # Same defensive clamp as fetch_current_weather/fetch_forecast_weather.
    temperature_c = min(max(temperature_c, 0.0), 45.0)
    humidity_pct = min(max(humidity_pct, 0.0), 100.0)
    return {"temperature_c": temperature_c, "humidity_pct": humidity_pct}


def geocode_address(address):
    """Best-effort resolve a free-text farm address to (latitude, longitude), or None
    if the address can't be confidently placed or the request fails/times out.

    Never raises, same "best effort" contract as fetch_current_weather/
    fetch_forecast_weather — accounts.views.signup treats None as "couldn't place this
    address," not a reason to block account creation.

    Open-Meteo's geocoding endpoint only matches a query against a single gazetteer
    place name (e.g. "Libmanan") — it does not parse a compound address the way a
    farmer actually types one at signup ("Sitio Tabawan, Patag, Libmanan, Camarines
    Sur" or "san isidro libmanan camarines sur" both return zero results as a single
    query, verified against the live API). So each word is tried as its own candidate
    query instead, restricted to the Philippines (a reasonable bias — this is a
    single-country farm app, itikcare-spec.md). A candidate is only trusted if one of
    the *other* words in the address also appears in its admin1/admin2/admin3 (region/
    province/municipality) — otherwise a common barangay name can silently resolve to
    the wrong province, or even the wrong country (a bare "Patag" query's top result is
    a barangay in Bulacan, ~300km from Camarines Sur; a bare "San Isidro" query's top
    result is in Buenos Aires, Argentina). Nothing is guessed when no word corroborates
    another — that's still safer than the previous single-query behavior, which just
    silently failed on every realistic multi-word address.
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
                # Nothing else in the address to corroborate this word against. Only
                # trust it if it's the *sole* PH match for that word -- if the name is
                # shared by several places (like "Patag" or "San Isidro"), there's no
                # way to tell which one is meant, so it must be discarded rather than
                # guessed at.
                score = 1 if len(results) == 1 else 0
            if score > best_score:
                best_score = score
                best_result = result

    if best_result is None or best_score <= 0:
        return None

    return float(best_result["latitude"]), float(best_result["longitude"])
