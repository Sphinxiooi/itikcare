"""Inference orchestration: turns a freshly-saved DailyLog into a Forecast (+ downstream
Recommendations).

Mirrors the "pure logic vs. thin DB orchestration" split already used elsewhere in this
project: modelling logic lives in ``pipeline.py`` (DB-agnostic), this module is the
ORM-facing glue layer that loads the trained model artifact, pulls recent DailyLog rows,
and persists the result — the same role ``train_forecast_model.py`` plays for training and
``recommendations/engine.py`` plays for the rule engine.
"""

import logging
import subprocess
import sys
from datetime import date, datetime, timedelta
from decimal import Decimal

import joblib
import numpy as np
import pandas as pd
from django.conf import settings
from django.db import transaction

from farm.models import DailyLog
from farm.services import get_effective_coordinates
from farm.weather import fetch_forecast_weather
from recommendations.engine import generate_recommendations

from . import pipeline as ml
from .models import Forecast

logger = logging.getLogger(__name__)

MODEL_DIR = settings.BASE_DIR / "models"
RETRAIN_LOG_PATH = MODEL_DIR / "retrain.log"


class ModelNotTrainedError(Exception):
    """Raised when generate_forecast is called before train_forecast_model has ever run."""


def model_path_for(owner_id: int):
    """Per-owner model artifact path — there is no single global model any more."""
    return MODEL_DIR / f"forecast_model_{owner_id}.joblib"


def _load_artifact(model_path) -> dict:
    if not model_path.exists():
        raise ModelNotTrainedError(
            f"No trained model at {model_path}. Run "
            f"`python manage.py train_forecast_model --owner-id <id>` first."
        )
    return joblib.load(model_path)


def _build_feature_row(daily_log: DailyLog):
    """Single-row MODEL_FEATURES-ordered input for .predict(), plus the prior logs used
    for lag1/roll3 (kept so the resulting Forecast can be traced back to its inputs).

    Priors are looked up the same way pipeline.add_lag_features builds them at training
    time: up to the 3 most recent DailyLogs in the same flock/caging_period, dated before
    this one. Fewer than 3 (e.g. the first log of a caging period) is a real, expected
    case — not an error — so missing lag1/roll3 become NaN, which the trained pipeline's
    SimpleImputer fills in.
    """
    priors = list(
        DailyLog.objects.filter(
            flock=daily_log.flock,
            caging_period=daily_log.caging_period,
            date__lt=daily_log.date,
        ).order_by("-date")[:3]
    )
    lag1 = float(priors[0].egg_count) if priors else np.nan
    roll3 = (sum(float(p.egg_count) for p in priors) / len(priors)) if priors else np.nan

    row = {
        "flock_size": float(daily_log.flock_size),
        "flock_age_weeks": float(daily_log.flock_age_weeks),
        "feed_intake_kg": float(daily_log.feed_intake_kg),
        "temperature_c": float(daily_log.temperature_c),
        "humidity_pct": float(daily_log.humidity_pct),
        "lag1": lag1,
        "roll3": roll3,
    }
    X = pd.DataFrame([row], columns=ml.MODEL_FEATURES).astype("float64")
    return X, priors


def _predict_next_days(daily_pipeline, daily_log: DailyLog, priors, weather_by_date: dict) -> tuple[float, float, float]:
    """Recursively forecast forecast_date + 1/+2/+3 days by re-applying daily_pipeline
    three times, feeding each step's own prediction back in as the next step's lag1/roll3
    history feature -- standard iterative multi-step forecasting. Not a separately trained
    model: reuses the same daily_pipeline used for predicted_daily_yield.

    flock_size/flock_age_weeks/feed_intake_kg are carried forward unchanged from
    daily_log's own values for all 3 future days, since the farmer hasn't logged them yet.
    temperature_c/humidity_pct come from weather_by_date (keyed by real calendar date --
    see farm.weather.fetch_forecast_weather) when available, else are likewise carried
    forward from daily_log.

    From day+2 onward, roll3 is a mean of *predicted* (not actual) prior days, so
    prediction error compounds across the 3 steps -- day+3 is the least certain of the
    three. This is expected and worth being explicit about (thesis defense): these are
    best-effort projections, not equally-certain restatements of the same-day nowcast.

    history holds up to the 3 most recent known-or-predicted egg counts, most-recent
    first: starts as [today's actual, yesterday's actual, day-before's actual] (fewer if
    priors is short, e.g. a fresh caging period), then each new prediction is prepended
    before the next step, same "up to 3, not exactly 3" convention as add_lag_features.
    """
    history = [float(daily_log.egg_count)] + [float(p.egg_count) for p in priors[:2]]
    predictions = []
    for n in range(1, 4):
        target_date = daily_log.date + timedelta(days=n)
        weather = weather_by_date.get(target_date, {})
        temperature_c = weather.get("temperature_c", float(daily_log.temperature_c))
        humidity_pct = weather.get("humidity_pct", float(daily_log.humidity_pct))
        row = {
            "flock_size": float(daily_log.flock_size),
            "flock_age_weeks": float(daily_log.flock_age_weeks),
            "feed_intake_kg": float(daily_log.feed_intake_kg),
            "temperature_c": temperature_c,
            "humidity_pct": humidity_pct,
            "lag1": history[0],
            "roll3": sum(history[:3]) / len(history[:3]),
        }
        X = pd.DataFrame([row], columns=ml.MODEL_FEATURES).astype("float64")
        pred = max(float(daily_pipeline.predict(X)[0]), 0.0)
        predictions.append(pred)
        history.insert(0, pred)
    return tuple(predictions)


def generate_forecast(daily_log: DailyLog, model_path=None) -> Forecast:
    """Predict daily + tri-day yield for daily_log's own date and persist a Forecast.

    forecast_date is set to daily_log.date (not the day after) — the daily/tri-day models
    are trained to predict a day's own yield (and the following 3-day sum) from that day's
    own farm conditions plus recent laying history, so this is a same-day "nowcast" using
    the reading the farmer just entered. dashboard/views.py's trend chart already expects
    this: it shows "actual" (DailyLog.egg_count) and "predicted" (Forecast.predicted_daily_yield)
    side by side for the same date.

    update_or_create keeps this idempotent (re-generating for a date already forecast
    updates it in place rather than erroring against the flock+forecast_date uniqueness
    constraint). Regenerates the Forecast's Recommendations as the final step.

    Raises ModelNotTrainedError if no model artifact exists yet. Any other error (corrupt
    artifact, shape mismatch, etc.) propagates — callers that must not lose the DailyLog
    save (farm.views.log_daily_data) should catch broadly around this call.

    Also persists a recursive best-effort day+1/day+2/day+3 breakdown (see
    _predict_next_days) for the dashboard's "Next 3-Day Forecast" panel — genuinely
    distinct per-day numbers, unlike predicted_tri_day_yield's 3-day sum above. Weather
    for those future days is only fetched when daily_log.date is today (Open-Meteo's
    forecast is anchored to real "now", so it can't meaningfully inform a backdated log's
    future days); otherwise the recursion falls back to daily_log's own carried-forward
    temperature_c/humidity_pct.
    """
    artifact = _load_artifact(model_path or model_path_for(daily_log.flock.owner_id))
    X, priors = _build_feature_row(daily_log)

    daily_pred = max(float(artifact["daily_pipeline"].predict(X)[0]), 0.0)
    tri_pred = max(float(artifact["tri_day_pipeline"].predict(X)[0]), 0.0)

    if daily_log.date == date.today():
        lat, lon = get_effective_coordinates(daily_log.flock.owner)
        weather_by_date = fetch_forecast_weather(lat, lon)
    else:
        weather_by_date = {}
    day1_pred, day2_pred, day3_pred = _predict_next_days(
        artifact["daily_pipeline"], daily_log, priors, weather_by_date
    )

    with transaction.atomic():
        forecast, _ = Forecast.objects.update_or_create(
            flock=daily_log.flock,
            forecast_date=daily_log.date,
            defaults={
                "predicted_daily_yield": Decimal(str(round(daily_pred, 2))),
                "predicted_tri_day_yield": Decimal(str(round(tri_pred, 2))),
                "predicted_next_day1_yield": Decimal(str(round(day1_pred, 2))),
                "predicted_next_day2_yield": Decimal(str(round(day2_pred, 2))),
                "predicted_next_day3_yield": Decimal(str(round(day3_pred, 2))),
                "feature_importances": artifact["feature_importances"]["daily"],
                "model_version": artifact["model_version"],
            },
        )
        forecast.source_logs.set([daily_log, *priors])
        generate_recommendations(forecast)

    return forecast


def trigger_retrain(reason: str, owner_id: int) -> None:
    """Fire-and-forget a background `train_forecast_model --owner-id ID --tune --strict` run.

    Called right after a DailyLog write closes out a caging period or retires a flock —
    the two points where a genuinely new, complete segment of training data exists (see
    itikcare-spec.md section 5's "rolling retraining as new data comes in"). Calendar-based
    retraining doesn't fit this farm's data-generation rhythm: it logs roughly a row a day,
    so a fixed interval would often fire with nothing new to learn from.

    ``owner_id`` scopes the retrain to one farmer's model only — each farm's own
    caging_period_closed/flock_retired events fire independently now, so this must never
    retrain (or overwrite the artifact of) every farm at once.

    Runs as a detached subprocess rather than inline because a --tune search is too slow
    (single-digit-to-low-tens of seconds) to run synchronously inside a request/response
    cycle. --strict is always used so a bad or unlucky retrain can never overwrite the
    model currently serving real predictions — it only persists if every acceptance
    threshold still passes; the previous working artifact is left untouched otherwise.
    (Contrast with the signup flow's bootstrap train, which runs synchronously and
    untuned — see accounts/views.py::signup — because that one must finish before the
    farmer's very first daily log can be forecast.)

    Never raises: launching the subprocess is best-effort, logged on failure, so a problem
    here can never break the view that triggered it.
    """
    try:
        RETRAIN_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with open(RETRAIN_LOG_PATH, "a", encoding="utf-8") as log_fh:
            log_fh.write(
                f"\n=== retrain triggered: reason={reason} owner_id={owner_id} "
                f"at {datetime.now().isoformat(timespec='seconds')} ===\n"
            )
            log_fh.flush()
            subprocess.Popen(
                [
                    sys.executable, str(settings.BASE_DIR / "manage.py"), "train_forecast_model",
                    "--owner-id", str(owner_id), "--tune", "--strict",
                ],
                stdout=log_fh,
                stderr=subprocess.STDOUT,
            )
    except Exception:
        logger.exception("Failed to launch background retrain (reason=%s, owner_id=%s)", reason, owner_id)
