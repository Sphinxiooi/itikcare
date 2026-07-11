"""Live weather lookup used only to suggest a starting temperature_c/humidity_pct value
on the daily log form (see views.log_daily_data) — never a substitute for the farmer's
own reading (itikcare-spec.md section 7: temperature/humidity are always manual entry).
The farmer still reviews, can overwrite, and must submit the form themselves.
"""

import logging

import requests
from django.conf import settings

logger = logging.getLogger(__name__)

OPEN_METEO_URL = "https://api.open-meteo.com/v1/forecast"
REQUEST_TIMEOUT_SECONDS = 5


def fetch_current_weather():
    """Best-effort fetch of current temperature (°C) / relative humidity (%) at the
    farm's fixed coordinates (FARM_LATITUDE/FARM_LONGITUDE).

    Returns {"temperature_c": float, "humidity_pct": float}, rounded to 1 decimal and
    clamped to DailyLogForm's widget ranges (0-45 / 0-100), or None if coordinates
    aren't configured, the request fails/times out, or the response is malformed.
    Never raises: farm connectivity is expected to be spotty (rural, semi-intensive
    husbandry), so callers can always safely treat None as "leave the field blank for
    the farmer to fill in," same as before this existed.
    """
    latitude = settings.FARM_LATITUDE
    longitude = settings.FARM_LONGITUDE
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
